"""One-off end-to-end check of the Knowledge pipeline, bypassing the HTTP layer.

Creates a small markdown file for the first agent found, indexes it (real OpenAI
embedding call on the workspace key), runs a similarity search, exercises the
search_knowledge tool handler, then deletes the test file again.
"""

import asyncio

from sqlmodel import select

from app.core import knowledge
from app.db.models import Agent, AgentKnowledgeFile
from app.db.session import AsyncSessionLocal

DOC = """# Refund policy

Customers can request a full refund within 30 days of purchase. After 30 days,
we offer store credit only. Refunds for annual plans are prorated by month.

# Shipping

Standard shipping takes 3-5 business days inside the EU. Express shipping is
available for 12 EUR and arrives the next business day.

# Support hours

The support desk is staffed Monday to Friday, 09:00 to 18:00 CET. Outside those
hours an on-call engineer handles emergencies only.
"""


async def main() -> None:
    async with AsyncSessionLocal() as db:
        agent = (await db.exec(select(Agent))).first()
        if agent is None:
            print("no agent to test with")
            return
        print(f"agent: {agent.name} ({agent.id})")

        text = knowledge.extract_text("policies.md", DOC.encode())
        file = AgentKnowledgeFile(
            agent_id=agent.id, org_id=agent.org_id,
            filename="policies.md", size_bytes=len(DOC), text=text,
        )
        db.add(file)
        await db.commit()
        await db.refresh(file)

        await knowledge.index_file(db, file)
        await db.refresh(file)
        print(f"index: status={file.status.value} chunks={file.chunk_count} error={file.error}")

        if file.status.value == "ready":
            results = await knowledge.search(db, agent.id, "how long do refunds take?")
            print(f"search: {len(results)} result(s); top: {results[0]['content'][:80]!r}")

            tool = await knowledge.build_tool(db, agent.id)
            print(f"tool registered: {tool.spec.name}")
            answer = await tool.handler({"query": "when is support available"}, False)
            print(f"tool answer starts: {answer[:100]!r}")

        await db.delete(file)
        await db.commit()
        print("test file cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
