"""Slice 2 smoke test — real connectors, real API calls.

Builds an agent wired to the workspace's actual Gmail and Telegram accounts, then checks the
three properties that make an acting agent safe to ship: a dry run touches nothing, a live run
does the work, and a second live run does not redo it.

Sends real Telegram messages. Reads, but never sends, real email.

    python tests/e2e_slice2.py
"""

import asyncio
import sys

from sqlmodel import delete, select

from app.core.agents.base import run_agent, snapshot_config
from app.db.models import (
    Agent,
    AgentProcessedItem,
    AgentSession,
    AgentSessionMessage,
    AgentTool,
    Connector,
    ConnectorType,
    Organization,
    SessionStatus,
    TriggerType,
    User,
)
from app.db.session import AsyncSessionLocal, engine
from app.integrations.registry import build_tools_for_agent

# The dev engine echoes every statement, which buries the checks this script exists to show.
# `echo` bypasses logger levels, so it has to be turned off on the engine itself.
engine.echo = False

INSTRUCTIONS = """You triage email.

When run, read the unread email. For each message, decide if it is urgent — something a human
must see today, like a customer complaint, a payment failure, or a direct question from a
person. Ignore newsletters, receipts, and automated notifications.

Send exactly one Telegram message summarising what you found, then stop. If nothing is
urgent, say so in one line. Never reply to email.
"""

PASS, FAIL = "  PASS", "  FAIL"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def trace(db, session: AgentSession) -> list[AgentSessionMessage]:
    msgs = list(
        (
            await db.exec(
                select(AgentSessionMessage)
                .where(AgentSessionMessage.session_id == session.id)
                .order_by(AgentSessionMessage.sequence)
            )
        ).all()
    )
    took = (session.finished_at - session.started_at).total_seconds()
    print(f"    {session.status.value} · {session.total_tokens} tokens · {session.iterations} steps · {took:.1f}s")
    if session.error:
        print(f"    error: {session.error}")
    for m in msgs:
        label = m.tool_name or m.role.value
        print(f"      [{m.sequence}] {label:<24} {m.content.replace(chr(10), ' ')[:90]}")
    return msgs


async def processed_count(db, agent_id) -> int:
    rows = await db.exec(
        select(AgentProcessedItem.id).where(AgentProcessedItem.agent_id == agent_id)
    )
    return len(list(rows.all()))


async def main() -> int:
    async with AsyncSessionLocal() as db:
        org = (await db.exec(select(Organization))).first()
        user = (await db.exec(select(User))).first()

        async def connector_of(kind: ConnectorType) -> Connector | None:
            return (await db.exec(select(Connector).where(Connector.type == kind))).first()

        llm = await connector_of(ConnectorType.anthropic) or await connector_of(ConnectorType.openai)
        gmail_c = await connector_of(ConnectorType.gmail)
        tg = await connector_of(ConnectorType.telegram_bot)

        if not (org and user and llm and gmail_c and tg):
            print("Need an org, user, an LLM connector, Gmail, and a Telegram bot. Connect them first.")
            return 1

        print(f"model    : {llm.name}")
        print(f"gmail    : {gmail_c.name}")
        print(f"telegram : {tg.name}")

        agent = Agent(
            org_id=org.id,
            created_by=user.id,
            name="Slice 2 smoke test",
            instructions=INSTRUCTIONS,
            model_connector_id=llm.id,
            settings={"max_iterations": 8},
        )
        agent.published_config = snapshot_config(agent)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        agent_id = agent.id
        db.add(AgentTool(agent_id=agent_id, connector_id=gmail_c.id))
        db.add(AgentTool(agent_id=agent_id, connector_id=tg.id))
        await db.commit()

        try:
            print("\n=== tool resolution ===")
            tools, contexts = await build_tools_for_agent(db, agent_id)
            names = sorted(t.spec.name for t in tools)
            print(f"    {names}")
            check("gmail + telegram tools resolved", len(contexts) == 2)
            check("read_unread_emails present", "read_unread_emails" in names)
            check("send_telegram_message present", "send_telegram_message" in names)
            check("no duplicate tool names", len(names) == len(set(names)))

            print("\n=== dry run ===")
            before = await processed_count(db, agent_id)
            s = await run_agent(db, agent, trigger_type=TriggerType.manual, dry_run=True)
            msgs = await trace(db, s)
            check("dry run succeeded", s.status == SessionStatus.succeeded, s.error or "")
            check("dry run called tools", any(m.tool_name for m in msgs))
            check(
                "dry run simulated every tool result",
                all(m.content.startswith("[simulated]") for m in msgs if m.tool_name),
            )
            check(
                "dry run recorded nothing as processed",
                await processed_count(db, agent_id) == before,
            )

            print("\n=== live run (sends a real Telegram message) ===")
            s = await run_agent(db, agent, trigger_type=TriggerType.manual)
            msgs = await trace(db, s)
            after_first = await processed_count(db, agent_id)
            check("live run succeeded", s.status == SessionStatus.succeeded, s.error or "")
            check(
                "live run sent a Telegram message",
                any(m.tool_name == "send_telegram_message" for m in msgs),
            )
            check("live run marked email as processed", after_first > before, f"{after_first} items")

            print("\n=== second live run (should find nothing new) ===")
            s = await run_agent(db, agent, trigger_type=TriggerType.manual)
            msgs = await trace(db, s)
            reads = [m for m in msgs if m.tool_name == "read_unread_emails"]
            check("second run succeeded", s.status == SessionStatus.succeeded, s.error or "")
            check(
                "second run saw no new email",
                bool(reads) and "No new unread email" in reads[0].content,
                reads[0].content[:80] if reads else "read tool was never called",
            )
            check(
                "second run added no new processed items",
                await processed_count(db, agent_id) == after_first,
            )

        finally:
            print("\n=== cleanup ===")
            sids = list(
                (await db.exec(select(AgentSession.id).where(AgentSession.agent_id == agent_id))).all()
            )
            # Processed items reference sessions, so they go first.
            await db.exec(delete(AgentProcessedItem).where(AgentProcessedItem.agent_id == agent_id))
            if sids:
                await db.exec(
                    delete(AgentSessionMessage).where(AgentSessionMessage.session_id.in_(sids))
                )
            await db.exec(delete(AgentSession).where(AgentSession.agent_id == agent_id))
            await db.exec(delete(AgentTool).where(AgentTool.agent_id == agent_id))
            await db.exec(delete(Agent).where(Agent.id == agent_id))
            await db.commit()
            print("  test agent, sessions, and processed items removed")

    await engine.dispose()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
