"""One-off: tear down channel triggers after the feature was switched off (2026-08-26).

Deletes every channel trigger row and calls deleteWebhook for each Telegram bot connector,
so Telegram stops queueing updates against a tunnel URL that no longer exists.
"""

import asyncio

import httpx
from sqlmodel import select

from app.core.crypto import decrypt_json
from app.db.models import AgentTrigger, Connector, ConnectorType, TriggerType
from app.db.session import AsyncSessionLocal


async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.exec(
            select(AgentTrigger).where(AgentTrigger.type == TriggerType.channel)
        )
        triggers = result.all()
        for trigger in triggers:
            await db.delete(trigger)
        await db.commit()
        print(f"deleted {len(triggers)} channel trigger(s)")

        result = await db.exec(
            select(Connector).where(Connector.type == ConnectorType.telegram_bot)
        )
        bots = result.all()

    async with httpx.AsyncClient(timeout=15) as client:
        for bot in bots:
            token = decrypt_json(bot.config).get("bot_token")
            if not token:
                continue
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/deleteWebhook",
                json={"drop_pending_updates": True},
            )
            print(f"deleteWebhook for {bot.name}: {resp.json()}")


if __name__ == "__main__":
    asyncio.run(main())
