"""Slice 1 smoke test — drives the runtime directly, no HTTP.

Creates a throwaway agent against whichever LLM connectors the workspace already has,
runs it with and without tools, and prints the recorded session. Cleans up after itself.

    python tests/e2e_slice1.py
"""

import asyncio
import sys

from sqlmodel import delete, select

from app.core.agents.base import RegisteredTool, run_agent, snapshot_config
from app.core.llm.client import ToolSpec
from app.db.models import (
    Agent,
    AgentSession,
    AgentSessionMessage,
    Connector,
    ConnectorType,
    Organization,
    TriggerType,
    User,
)
from app.db.session import AsyncSessionLocal, engine

LLM_TYPES = [ConnectorType.openai, ConnectorType.anthropic]


def fake_weather_tool() -> RegisteredTool:
    async def handler(args: dict, dry_run: bool) -> str:
        # Handlers describe the action plainly; the runner adds the [simulated] marker.
        if dry_run:
            return f"Would have looked up the weather for {args.get('city')}."
        return '{"temp_c": -5, "conditions": "snow"}'

    return RegisteredTool(
        spec=ToolSpec(
            name="get_weather",
            description="Get the current weather for a city.",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        ),
        handler=handler,
    )


async def show(db, session: AgentSession) -> None:
    msgs = await db.exec(
        select(AgentSessionMessage)
        .where(AgentSessionMessage.session_id == session.id)
        .order_by(AgentSessionMessage.sequence)
    )
    took = (session.finished_at - session.started_at).total_seconds()
    print(f"    status    : {session.status.value}")
    print(f"    name      : {session.name!r}")
    print(f"    tokens    : {session.total_tokens}  iterations: {session.iterations}  {took:.1f}s")
    if session.error:
        print(f"    error     : {session.error}")
    for m in msgs.all():
        label = m.tool_name or m.role.value
        body = m.content.replace("\n", " ")[:70]
        print(f"      [{m.sequence}] {label:<14} {body}")


async def main() -> int:
    async with AsyncSessionLocal() as db:
        org = (await db.exec(select(Organization))).first()
        user = (await db.exec(select(User))).first()
        connectors = (await db.exec(select(Connector).where(Connector.type.in_(LLM_TYPES)))).all()

        if not (org and user and connectors):
            print("Need an org, a user, and at least one LLM connector. Log in first.")
            return 1

        created: list[Agent] = []
        for connector in connectors:
            print(f"\n=== {connector.type.value} ({connector.name}) ===")

            agent = Agent(
                org_id=org.id,
                created_by=user.id,
                name=f"Smoke test · {connector.type.value}",
                instructions="You are a concise assistant. Keep answers under 20 words.",
                model_connector_id=connector.id,
            )
            agent.published_config = snapshot_config(agent)
            db.add(agent)
            await db.commit()
            await db.refresh(agent)
            created.append(agent)

            print("  -- no tools --")
            s = await run_agent(
                db, agent, trigger_type=TriggerType.manual, user_input="What is the capital of Canada?"
            )
            await show(db, s)

            print("  -- with a tool, dry run --")
            s = await run_agent(
                db, agent,
                trigger_type=TriggerType.manual,
                user_input="What is the weather in Ottawa?",
                tools=[fake_weather_tool()],
                dry_run=True,
            )
            await show(db, s)

            print("  -- with a tool, live --")
            s = await run_agent(
                db, agent,
                trigger_type=TriggerType.manual,
                user_input="What is the weather in Ottawa?",
                tools=[fake_weather_tool()],
            )
            await show(db, s)

            print("  -- unknown tool the agent cannot satisfy --")
            s = await run_agent(
                db, agent,
                trigger_type=TriggerType.manual,
                user_input="Send an email to bob@example.com saying hello.",
                tools=[fake_weather_tool()],
            )
            await show(db, s)

            print("  -- max_iterations guard (limit 1, tool required) --")
            agent.settings = {**agent.settings, "max_iterations": 1}
            agent.published_config = snapshot_config(agent)
            db.add(agent)
            await db.commit()
            s = await run_agent(
                db, agent,
                trigger_type=TriggerType.manual,
                user_input="What is the weather in Ottawa? Use the tool.",
                tools=[fake_weather_tool()],
            )
            await show(db, s)

        print("\n=== cleanup ===")
        for agent in created:
            sids = list((await db.exec(select(AgentSession.id).where(AgentSession.agent_id == agent.id))).all())
            if sids:
                await db.exec(delete(AgentSessionMessage).where(AgentSessionMessage.session_id.in_(sids)))
            await db.exec(delete(AgentSession).where(AgentSession.agent_id == agent.id))
            await db.delete(agent)
        await db.commit()
        print(f"  removed {len(created)} test agents and their sessions")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
