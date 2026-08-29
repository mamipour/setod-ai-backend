"""
Owner Notes
===========
Short owner-authored text injected verbatim into every agent run's system
prompt. Two kinds, unified in one table:

  Fact   — expires by date or never. Agents only read.
  Task   — agent_resolvable=True. Expires when an agent claims it via
           `mark_note_done`. The claim is a conditional UPDATE so two
           concurrent agents cannot both act on the same task.

Notes bypass the publish snapshot — changes take effect on the next run
without a republish.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import OwnerNote


MAX_BODY = 500
MAX_LIVE_PER_ORG = 50


async def live_notes(
    db: AsyncSession,
    org_id: UUID,
    agent_id: UUID,
) -> list[OwnerNote]:
    """Return unresolved, unexpired notes visible to this agent.

    Scope filter: agent_ids is null (all agents) OR contains this agent's id.
    The list is loaded in full and filtered in Python — notes are capped at 50
    per org, so this is always a small result set.
    """
    now = datetime.now(UTC)
    result = await db.exec(
        select(OwnerNote)
        .where(
            OwnerNote.org_id == org_id,
            OwnerNote.resolved_at.is_(None),
            (OwnerNote.expires_at.is_(None)) | (OwnerNote.expires_at > now),
        )
        .order_by(OwnerNote.created_at)
    )
    rows = result.all()
    agent_str = str(agent_id)
    return [
        n for n in rows
        if n.agent_ids is None or agent_str in n.agent_ids
    ]


def prompt_block(notes: list[OwnerNote]) -> str | None:
    """Render notes as a system-prompt block.

    Facts and tasks go into separate labelled sections. Tasks include a short
    id (first 8 chars) so the model can reference them in mark_note_done.
    Returns None when the list is empty.
    """
    if not notes:
        return None

    facts = [n for n in notes if not n.agent_resolvable]
    tasks = [n for n in notes if n.agent_resolvable]

    parts: list[str] = []
    if facts:
        lines = "\n".join(f"- {n.body}" for n in facts)
        parts.append(f"WORKSPACE NOTES (from the owner — treat as current policy):\n{lines}")
    if tasks:
        lines = "\n".join(
            f"- [task:{str(n.id)[:8]}] {n.body}"
            for n in tasks
        )
        parts.append(
            "OPEN TASKS (owner-created — use mark_note_done with the task id when "
            "you have handled the task; do not act on it if it is already done):\n"
            + lines
        )
    return "\n\n".join(parts)


async def build_tool(
    db: AsyncSession,
    org_id: UUID,
    agent_id: UUID,
) -> "RegisteredTool | None":
    """Return the mark_note_done tool, or None when there are no open tasks.

    Absent rather than present-but-empty: a model offered a tool will try it,
    and an agent with no resolvable tasks should not spend an iteration
    learning that.
    """
    from app.core.agents.base import RegisteredTool
    from app.core.llm.client import ToolSpec

    notes = await live_notes(db, org_id, agent_id)
    tasks = [n for n in notes if n.agent_resolvable]
    if not tasks:
        return None

    async def handler(args: dict, dry_run: bool) -> str:
        note_id_raw = str(args.get("note_id") or "").strip()
        resolution = str(args.get("note") or "").strip()
        if not note_id_raw:
            return "Provide the task id (the part after 'task:' shown in OPEN TASKS)."

        # Accept both the 8-char prefix and the full UUID.
        matched: OwnerNote | None = None
        for task in tasks:
            full = str(task.id)
            if full.startswith(note_id_raw) or full == note_id_raw:
                matched = task
                break
        if matched is None:
            return f"Task id '{note_id_raw}' not found in open tasks."

        if dry_run:
            return f"[simulated] Would mark task '{matched.body[:60]}' done."

        # Claim via conditional UPDATE — rowcount 0 means a concurrent agent
        # already resolved it. Built through the ORM rather than raw SQL so the
        # UUID columns get real uuid binds; string binds are sent as varchar and
        # Postgres refuses to compare those against a uuid column.
        result = await db.execute(
            update(OwnerNote)
            .where(OwnerNote.id == matched.id, OwnerNote.resolved_at.is_(None))
            .values(
                resolved_at=datetime.now(UTC),
                resolved_by=agent_id,
                resolution=resolution[:500],
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()

        if result.rowcount == 0:
            return "Already handled by another agent — do not act on this task again."
        return f"Marked task done: '{matched.body[:60]}'."

    return RegisteredTool(
        spec=ToolSpec(
            name="mark_note_done",
            description=(
                "Mark an open owner task as done. Use the task id shown next to the "
                "task in OPEN TASKS (the part after 'task:'). Include a brief note "
                "on what you did."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note_id": {
                        "type": "string",
                        "description": "The task id (8-char prefix or full UUID).",
                    },
                    "note": {
                        "type": "string",
                        "description": "One sentence: what you did to handle this task.",
                    },
                },
                "required": ["note_id", "note"],
            },
        ),
        handler=handler,
    )
