"""Slice 5 smoke test — owner notes and agent-to-agent calls.

Exercises note scoping/expiry, the claim-safe task resolve, and the call_* tool
builder against a real database. No LLM calls: the tool builders only assemble
specs, and handlers are driven directly, so dry-run paths never start a run.
Creates an isolated org and deletes everything on the way out.

    python tests/e2e_slice5.py
"""

import asyncio
import sys
from datetime import UTC, datetime
from uuid import uuid4

from sqlmodel import delete, select

from app.core import notes as notes_mod
from app.core.agents import calls as calls_mod
from app.db.models import (
    Agent,
    AgentLink,
    AgentStatus,
    Organization,
    OwnerNote,
    User,
)
from app.db.session import AsyncSessionLocal, engine

engine.echo = False
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'  PASS' if ok else '  FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> None:
    org_id = uuid4()
    async with AsyncSessionLocal() as db:
        org = Organization(
            id=org_id, name="ZZ Review Org", slug=f"zz-review-{org_id.hex[:8]}"
        )
        db.add(org)
        user = User(
            id=uuid4(),
            google_id=f"zz-review-{org_id.hex[:8]}",
            email=f"zz-review-{org_id.hex[:8]}@example.test",
            name="ZZ Review",
        )
        db.add(user)
        await db.commit()

        caller = Agent(
            id=uuid4(), org_id=org_id, created_by=user.id,
            name="Cancel Handler", status=AgentStatus.published,
        )
        published_target = Agent(
            id=uuid4(), org_id=org_id, created_by=user.id,
            name="Booking Agent", status=AgentStatus.published,
        )
        draft_target = Agent(
            id=uuid4(), org_id=org_id, created_by=user.id,
            name="Draft Agent", status=AgentStatus.draft,
        )
        db.add_all([caller, published_target, draft_target])
        await db.commit()

        print("\n── notes.live_notes scoping ──")
        all_agents_note = OwnerNote(
            org_id=org_id, created_by=user.id, body="Front desk closes at 5pm.",
            agent_ids=None,
        )
        scoped_note = OwnerNote(
            org_id=org_id, created_by=user.id, body="Only booking agent sees this.",
            agent_ids=[str(published_target.id)],
        )
        expired_note = OwnerNote(
            org_id=org_id, created_by=user.id, body="Expired fact.",
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        task_note = OwnerNote(
            org_id=org_id, created_by=user.id,
            body="Kevin is waiting for a booking window.",
            agent_resolvable=True,
        )
        db.add_all([all_agents_note, scoped_note, expired_note, task_note])
        await db.commit()

        live_caller = await notes_mod.live_notes(db, org_id, caller.id)
        bodies = {n.body for n in live_caller}
        check("all-agents note visible to caller", "Front desk closes at 5pm." in bodies)
        check("scoped note hidden from caller", "Only booking agent sees this." not in bodies)
        check("expired note excluded", "Expired fact." not in bodies)
        check("task note visible", "Kevin is waiting for a booking window." in bodies)

        live_target = await notes_mod.live_notes(db, org_id, published_target.id)
        check(
            "scoped note visible to its agent",
            "Only booking agent sees this." in {n.body for n in live_target},
        )

        other_org_live = await notes_mod.live_notes(db, uuid4(), caller.id)
        check("notes do not leak across orgs", other_org_live == [], f"got {len(other_org_live)}")

        print("\n── notes.prompt_block ──")
        block = notes_mod.prompt_block(live_caller)
        check("block mentions WORKSPACE NOTES", "WORKSPACE NOTES" in (block or ""))
        check("block mentions OPEN TASKS", "OPEN TASKS" in (block or ""))
        check(
            "task rendered with 8-char id",
            f"[task:{str(task_note.id)[:8]}]" in (block or ""),
        )
        check("empty list yields None", notes_mod.prompt_block([]) is None)

        print("\n── notes.build_tool + claim safety ──")
        tool = await notes_mod.build_tool(db, org_id, caller.id)
        check("mark_note_done tool built", tool is not None and tool.spec.name == "mark_note_done")

        dry = await tool.handler({"note_id": str(task_note.id)[:8], "note": "x"}, True)
        check("dry run does not resolve", "[simulated]" in dry, dry)

        bad = await tool.handler({"note_id": "deadbeef", "note": "x"}, False)
        check("unknown task id rejected", "not found" in bad, bad)

        first = await tool.handler(
            {"note_id": str(task_note.id)[:8], "note": "Booked Kevin at 3pm."}, False
        )
        check("first claim succeeds", "Marked task done" in first, first)

        second = await tool.handler(
            {"note_id": str(task_note.id)[:8], "note": "Booked again."}, False
        )
        check("second claim rejected (claim-safe)", "Already handled" in second, second)

        await db.refresh(task_note)
        check("resolved_at persisted", task_note.resolved_at is not None)
        check(
            "resolved_by persisted as UUID",
            task_note.resolved_by == caller.id,
            f"got {task_note.resolved_by!r}",
        )
        check("resolution persisted", task_note.resolution == "Booked Kevin at 3pm.",
              repr(task_note.resolution))

        after = await notes_mod.live_notes(db, org_id, caller.id)
        check(
            "resolved task drops out of live notes",
            "Kevin is waiting for a booking window." not in {n.body for n in after},
        )

        tool_after = await notes_mod.build_tool(db, org_id, caller.id)
        check("tool absent once no open tasks", tool_after is None)

        print("\n── calls.build_tools ──")
        empty = await calls_mod.build_tools(db, caller, uuid4())
        check("no links yields no tools", empty == [], f"got {len(empty)}")

        db.add(AgentLink(
            agent_id=caller.id, target_agent_id=published_target.id,
            description="Use when a freed slot should be offered to a waiting customer.",
        ))
        db.add(AgentLink(
            agent_id=caller.id, target_agent_id=draft_target.id,
            description="Should be skipped — target is a draft.",
        ))
        await db.commit()

        built = await calls_mod.build_tools(db, caller, uuid4())
        names = [t.spec.name for t in built]
        check("published target produces one tool", len(built) == 1, f"got {names}")
        check("tool name slugified", names == ["call_booking_agent"], f"got {names}")
        check(
            "description passed through verbatim",
            built[0].spec.description
            == "Use when a freed slot should be offered to a waiting customer.",
        )
        check(
            "message param required",
            built[0].spec.parameters.get("required") == ["message"],
        )

        blank = await built[0].handler({"message": "   "}, False)
        check("blank message rejected before any run", "Provide a message" in blank, blank)

        dry_call = await built[0].handler({"message": "Offer 3pm to Kevin."}, True)
        check(
            "dry run does not start a real run",
            "simulated" in dry_call.lower() or "would" in dry_call.lower(),
            dry_call,
        )

    # Cleanup
    async with AsyncSessionLocal() as db:
        await db.exec(delete(AgentLink).where(AgentLink.agent_id.in_(
            select(Agent.id).where(Agent.org_id == org_id)
        )))
        await db.exec(delete(OwnerNote).where(OwnerNote.org_id == org_id))
        await db.exec(delete(Agent).where(Agent.org_id == org_id))
        await db.exec(delete(User).where(User.email.like(f"zz-review-{org_id.hex[:8]}%")))
        await db.exec(delete(Organization).where(Organization.id == org_id))
        await db.commit()
    print("\ncleaned up test org")

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}"))
    sys.exit(1 if failures else 0)


asyncio.run(main())
