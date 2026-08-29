"""Slice 4 smoke test — the API path the agent builder UI walks.

Drives the create flow over HTTP exactly as the browser does: list templates, create from one,
attach accounts, add a trigger, dry run, publish. Authentication is stubbed to the first real
user in the database, so this exercises routing and serialisation without a login round trip.

    python tests/e2e_slice4.py
"""

import asyncio
import sys

import httpx
from sqlmodel import delete, select

from app.api.auth.dependencies import get_current_user
from app.db.models import (
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentTool,
    AgentTrigger,
    Connector,
    ConnectorType,
    Organization,
    User,
)
from app.db.session import AsyncSessionLocal, engine
from app.main import app

engine.echo = False

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'  PASS' if ok else '  FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> int:
    async with AsyncSessionLocal() as db:
        org = (await db.exec(select(Organization))).first()
        user = (await db.exec(select(User))).first()
        if not (org and user):
            print("Need an org and a user. Log in once first.")
            return 1

        async def connector_of(kind):
            return (await db.exec(select(Connector).where(Connector.type == kind))).first()

        llm = await connector_of(ConnectorType.anthropic) or await connector_of(ConnectorType.openai)
        gmail = await connector_of(ConnectorType.gmail)
        tg = await connector_of(ConnectorType.telegram_bot)

    app.dependency_overrides[get_current_user] = lambda: user
    transport = httpx.ASGITransport(app=app)
    agent_id = ""

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # ── Templates ─────────────────────────────────────────────────────
            print("\n=== template gallery ===")
            r = await c.get("/agents/templates", params={"org_id": str(org.id)})
            check("templates listed", r.status_code == 200, str(r.status_code))
            templates = r.json()
            check("at least one template returned", len(templates) >= 1, f"{len(templates)}")

            by_key = {t["key"]: t for t in templates}
            triage = by_key.get("support_triage")
            check("support_triage present", triage is not None)
            check(
                "template carries instructions the UI can show",
                bool(triage and len(triage["instructions"]) > 200),
            )
            check(
                "readiness reflects connected accounts",
                triage is not None and triage["ready"] == (gmail is not None),
                f"ready={triage['ready'] if triage else '?'}, gmail connected={gmail is not None}",
            )

            missed = by_key.get("missed_call")
            check(
                "missing connectors are named, not just counted",
                missed is not None
                and (missed["ready"] or "twilio" in missed["missing_connectors"]),
            )

            # ── Model list ────────────────────────────────────────────────────
            print("\n=== model list ===")
            if llm:
                r = await c.get("/agents/models", params={"connector_id": str(llm.id)})
                check("models listed for a provider connector", r.status_code == 200, r.text[:120])
                body = r.json()
                check(
                    "the provider returned a usable list",
                    len(body["models"]) > 0,
                    body.get("detail", ""),
                )
                check(
                    "every entry has an id and a label",
                    all(m.get("id") and m.get("label") for m in body["models"]),
                )
                check(
                    "non-chat models are filtered out",
                    not any(
                        "embedding" in m["id"] or "whisper" in m["id"] for m in body["models"]
                    ),
                )

            if gmail:
                r = await c.get("/agents/models", params={"connector_id": str(gmail.id)})
                check(
                    "asking a non-provider connector is refused",
                    r.status_code == 422,
                    str(r.status_code),
                )

            # ── Create from a template ────────────────────────────────────────
            print("\n=== create from template ===")
            r = await c.post(
                "/agents/",
                json={
                    "org_id": str(org.id),
                    "name": "Slice 4 smoke test",
                    "template_key": "support_triage",
                    "model_connector_id": str(llm.id) if llm else None,
                },
            )
            check("agent created", r.status_code == 201, r.text[:120])
            if r.status_code != 201:
                return 1
            agent = r.json()
            agent_id = agent["id"]

            check(
                "instructions pre-filled from the template",
                agent["instructions"] == triage["instructions"],
            )
            check("icon taken from the template", agent["icon"] == "inbox", agent["icon"])
            check("name the caller sent wins", agent["name"] == "Slice 4 smoke test")
            check("starts as a draft", agent["status"] == "draft")

            r = await c.post(
                "/agents/",
                json={"org_id": str(org.id), "template_key": "nope"},
            )
            check("unknown template is rejected", r.status_code == 404, str(r.status_code))

            # ── Attach accounts ───────────────────────────────────────────────
            print("\n=== accounts and tools ===")
            for connector in filter(None, [gmail, tg]):
                r = await c.post(
                    f"/agents/{agent_id}/tools", json={"connector_id": str(connector.id)}
                )
                check(f"attached {connector.type.value}", r.status_code == 201, r.text[:120])

            r = await c.get(f"/agents/{agent_id}/tools")
            tools = r.json()
            check("tools listed for the builder", r.status_code == 200)
            check(
                "each account reports its individual tools",
                all(len(t["tools"]) > 0 for t in tools),
            )
            check(
                "tools default to enabled",
                all(all(x["enabled"] for x in t["tools"]) for t in tools),
            )

            if tools:
                first = tools[0]
                keep = [t["name"] for t in first["tools"]][:1]
                r = await c.post(
                    f"/agents/{agent_id}/tools",
                    json={"connector_id": first["connector_id"], "enabled_tools": keep},
                )
                check("trimming the tool list works", r.status_code == 201, r.text[:120])
                enabled = [t["name"] for t in r.json()["tools"] if t["enabled"]]
                check("only the kept tool is enabled", enabled == keep, str(enabled))

            if llm is None:
                check("an LLM connector is available", False, "none connected")
                return 1

            # ── Triggers ──────────────────────────────────────────────────────
            print("\n=== triggers ===")
            r = await c.get("/agents/schedule-presets")
            check("schedule presets reachable", r.status_code == 200, str(r.status_code))
            presets = {p["key"] for p in r.json()}
            check("presets include a daily option", "daily_9am" in presets)

            r = await c.post(
                f"/agents/{agent_id}/triggers",
                json={"type": "schedule", "config": {"preset": "daily_9am"}},
            )
            check("schedule trigger created", r.status_code == 201, r.text[:120])
            trigger = r.json()
            check("trigger has a next run", trigger["next_run_at"] is not None)
            check(
                "trigger summarises itself in plain language",
                "Daily 9am" in trigger["summary"],
                trigger["summary"],
            )

            r = await c.post(
                f"/agents/{agent_id}/triggers",
                json={"type": "schedule", "config": {"cron": "* * * * *"}},
            )
            check("a too-frequent schedule is refused", r.status_code == 422, str(r.status_code))
            check(
                "the refusal explains itself",
                "minutes apart" in r.text,
                r.json().get("detail", "")[:80],
            )

            r = await c.patch(
                f"/agents/{agent_id}/triggers/{trigger['id']}",
                json={"type": "schedule", "config": {"preset": "daily_9am"}, "enabled": False},
            )
            check("pausing clears the next run", r.json()["next_run_at"] is None)

            # ── Publish gating and dry run ────────────────────────────────────
            print("\n=== run and publish ===")
            r = await c.post(
                f"/agents/{agent_id}/run",
                json={"org_id": str(org.id), "dry_run": True},
            )
            check(
                "an unpublished agent cannot be run live",
                r.status_code == 422,
                str(r.status_code),
            )

            r = await c.post(
                f"/agents/{agent_id}/run",
                json={"org_id": str(org.id), "dry_run": True, "use_draft": True},
            )
            check("the draft can be test-run", r.status_code == 200, r.text[:160])
            if r.status_code == 200:
                run = r.json()
                check("test run is marked as a dry run", run["dry_run"] is True)
                r = await c.get(f"/agents/{agent_id}/sessions/{run['id']}")
                detail = r.json()
                check("the run has a readable transcript", len(detail["messages"]) > 0)
                check(
                    "simulated actions are labelled",
                    all(
                        m["content"].startswith("[simulated]")
                        for m in detail["messages"]
                        if m["tool_name"]
                    ),
                )

            r = await c.post(f"/agents/{agent_id}/publish")
            check("agent published", r.status_code == 200, r.text[:120])
            check("status flips to published", r.json()["status"] == "published")
            check("no unpublished edits right after publishing",
                  r.json()["has_unpublished_changes"] is False)

            r = await c.patch(f"/agents/{agent_id}", json={"instructions": "Changed."})
            check(
                "editing a published agent flags unpublished changes",
                r.json()["has_unpublished_changes"] is True,
            )

            r = await c.get("/agents/", params={"org_id": str(org.id)})
            check(
                "the new agent shows in the list",
                any(a["id"] == agent_id for a in r.json()),
            )

    finally:
        app.dependency_overrides.clear()
        print("\n=== cleanup ===")
        if agent_id:
            async with AsyncSessionLocal() as db:
                sids = list(
                    (
                        await db.exec(
                            select(AgentSession.id).where(AgentSession.agent_id == agent_id)
                        )
                    ).all()
                )
                if sids:
                    await db.exec(
                        delete(AgentSessionMessage).where(
                            AgentSessionMessage.session_id.in_(sids)
                        )
                    )
                await db.exec(delete(AgentSession).where(AgentSession.agent_id == agent_id))
                await db.exec(delete(AgentTrigger).where(AgentTrigger.agent_id == agent_id))
                await db.exec(delete(AgentTool).where(AgentTool.agent_id == agent_id))
                await db.exec(delete(Agent).where(Agent.id == agent_id))
                await db.commit()
        print("  test agent removed")

    await engine.dispose()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
