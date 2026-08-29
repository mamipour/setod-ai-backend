"""
Integration foundation
======================
Shared plumbing for turning a connector into a set of tools an agent can call.

Each integration module exposes `build_tools(ctx) -> list[RegisteredTool]`. The registry
calls it once per `AgentTool` row and hands back a flat list to the runner.

Two conventions matter here.

**Aliasing.** One workspace can connect two Gmail accounts. If both are attached to the same
agent, the model cannot be offered two tools called `send_email` — it has no way to choose.
`AgentTool.alias` disambiguates them into `send_email_sales` and `send_email_support`, and
the alias is appended to the description too, since the name alone rarely says which account
is which.

**Idempotency.** Read tools filter against `AgentProcessedItem` on the way in, so a scheduled
agent never sees the same email twice without the model having to remember anything. What
gets written back depends on whether the action can be undone:

- `note_seen` is deferred and flushed by the runner only if the run succeeds. Merely *reading*
  an item is not worth remembering through a crash — better to surface it again next run than
  to drop work the agent never got to.
- `mark_processed` commits immediately, and is what irreversible actions use. Once a reply has
  actually been sent, that fact has to survive a crash three steps later, or the next run
  sends it a second time.
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.agents.base import RegisteredTool
from app.db.models import AgentProcessedItem, Connector, ProcessedItemStatus


class IntegrationError(RuntimeError):
    """A tool could not do its job. Surfaced to the model as the tool result."""


@dataclass
class ToolContext:
    """Everything a tool needs at build time."""

    db: AsyncSession
    agent_id: UUID
    connector: Connector
    alias: str = ""
    # External ids surfaced by read tools this run, flushed to AgentProcessedItem on success.
    seen: list[tuple[UUID, str]] = field(default_factory=list)

    def tool_name(self, base: str) -> str:
        return f"{base}_{slug(self.alias)}" if self.alias else base

    def describe(self, text: str) -> str:
        """Append which account a tool acts on, when the agent has more than one."""
        return f"{text} (account: {self.alias})" if self.alias else text

    def note_seen(self, external_id: str) -> None:
        """Record an item as surfaced. Persisted only if the run succeeds."""
        self.seen.append((self.connector.id, external_id))

    async def mark_processed(self, external_id: str) -> None:
        """Persist an item as permanently handled right now.

        Used by write tools (reply, archive, send) immediately after an irreversible
        action.  If an `in_flight` reservation for the same item already exists (e.g.
        the item was read in the same run and the write tool is approval-gated), this
        upgrades it to `permanent` in place rather than silently doing nothing.
        """
        stmt = (
            insert(AgentProcessedItem)
            .values(
                agent_id=self.agent_id,
                connector_id=self.connector.id,
                external_id=external_id,
                status=ProcessedItemStatus.permanent,
            )
            .on_conflict_do_update(
                constraint="uq_agent_processed_item",
                set_={"status": ProcessedItemStatus.permanent},
            )
        )
        await self.db.exec(stmt)
        await self.db.commit()

    async def unprocessed(self, external_ids: list[str]) -> set[str]:
        """Of these external ids, which has this agent not already claimed?

        Both `permanent` and `in_flight` rows are treated as claimed: a permanent row
        means a past run handled it, an in_flight row means the current or a concurrent
        run has reserved it.  Either way, this run should skip it.
        """
        if not external_ids:
            return set()
        rows = await self.db.exec(
            select(AgentProcessedItem.external_id).where(
                AgentProcessedItem.agent_id == self.agent_id,
                AgentProcessedItem.connector_id == self.connector.id,
                AgentProcessedItem.external_id.in_(external_ids),
            )
        )
        return set(external_ids) - set(rows.all())


def slug(value: str) -> str:
    """Tool names must match ^[a-zA-Z0-9_-]+$ for both providers."""
    cleaned = "".join(c if c.isalnum() else "_" for c in value.lower())
    return "_".join(filter(None, cleaned.split("_")))[:24]


__all__ = ["IntegrationError", "RegisteredTool", "ToolContext", "slug"]
