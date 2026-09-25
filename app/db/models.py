import secrets
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

# Store all datetimes as TIMESTAMPTZ (timezone-aware) in PostgreSQL
_ts = lambda: Field(sa_type=DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


# ── Enums ─────────────────────────────────────────────────────────────────────

class MemberRole(str, Enum):
    owner = "owner"
    member = "member"


class ConnectorType(str, Enum):
    gmail = "gmail"
    telegram_bot = "telegram_bot"
    telegram_client = "telegram_client"
    twilio = "twilio"
    webhook = "webhook"
    slack_webhook = "slack_webhook"
    google_sheets = "google_sheets"
    whatsapp = "whatsapp"
    openai = "openai"
    anthropic = "anthropic"
    mcp = "mcp"


class ConnectorStatus(str, Enum):
    active = "active"
    error = "error"
    pending_auth = "pending_auth"
    revoked = "revoked"


class AgentStatus(str, Enum):
    draft = "draft"
    published = "published"
    paused = "paused"  # published_config preserved; worker skips all runs until resumed


class TriggerType(str, Enum):
    schedule = "schedule"
    channel = "channel"
    manual = "manual"
    agent = "agent"  # run started by another agent calling this one as a tool


class SessionStatus(str, Enum):
    running = "running"
    succeeded = "succeeded"
    error = "error"
    waiting_approval = "waiting_approval"


class MessageRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


# ── User ──────────────────────────────────────────────────────────────────────

class User(SQLModel, table=True):
    __tablename__ = "users"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    google_id: str = Field(unique=True, index=True)
    email: str = Field(unique=True, index=True)
    name: str
    avatar_url: str | None = None
    created_at: datetime = _ts()
    updated_at: datetime = _ts()


# ── Organization ───────────────────────────────────────────────────────────────

class Organization(SQLModel, table=True):
    __tablename__ = "organizations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str
    slug: str = Field(unique=True, index=True)
    # Encrypted workspace-level integration settings. Currently holds:
    #   {"provider": "duckduckgo" | "tavily", "tavily_api_key": "<key>"}
    # Encrypted via Fernet so the key is never stored in plain text. None when
    # no workspace integrations have been configured.
    web_settings: str | None = Field(default=None)
    # Encrypted notification preferences. Holds:
    #   {"telegram_connector_id": "<uuid>" | null}
    # Email always falls back to the owner's login email via Resend.
    notify_settings: str | None = Field(default=None)
    created_at: datetime = _ts()
    updated_at: datetime = _ts()


# ── OrganizationMember ─────────────────────────────────────────────────────────

class OrganizationMember(SQLModel, table=True):
    __tablename__ = "organization_members"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    role: MemberRole = Field(default=MemberRole.member)
    invited_by_id: UUID | None = Field(default=None, foreign_key="users.id")
    joined_at: datetime = _ts()


# ── Invitation ─────────────────────────────────────────────────────────────────

INVITATION_EXPIRE_DAYS = 7


class Invitation(SQLModel, table=True):
    __tablename__ = "invitations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    email: str = Field(index=True)
    token: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32),
        unique=True,
        index=True,
    )
    role: MemberRole = Field(default=MemberRole.member)
    invited_by_id: UUID = Field(foreign_key="users.id")
    expires_at: datetime = Field(
        sa_type=DateTime(timezone=True),
        default_factory=lambda: datetime.now(UTC) + timedelta(days=INVITATION_EXPIRE_DAYS),
    )
    accepted_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = _ts()

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None and not self.is_expired


# ── Connector ──────────────────────────────────────────────────────────────────

class Connector(SQLModel, table=True):
    __tablename__ = "connectors"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    created_by: UUID = Field(foreign_key="users.id")
    name: str
    type: ConnectorType
    status: ConnectorStatus = Field(default=ConnectorStatus.pending_auth)
    config: str | None = Field(default=None)  # Fernet-encrypted JSON — never returned to client
    created_at: datetime = _ts()
    updated_at: datetime = _ts()


# ── Agent ──────────────────────────────────────────────────────────────────────

DEFAULT_AGENT_SETTINGS: dict[str, Any] = {
    "max_iterations": 10,
    "tool_concurrency": 3,
    "web_search": False,
    "web_search_provider": "native",
    "live_page_access": False,
    "search_context": "medium",
    "reasoning": False,
    "episodic_memory": False,
    "daily_token_budget": 500_000,
}


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    created_by: UUID = Field(foreign_key="users.id")
    name: str
    icon: str = Field(default="robot")
    instructions: str = Field(default="")
    template_key: str | None = Field(default=None)  # which template it was created from
    # Which credentials to use, and which model on that provider — the UI shows them together
    # ("GPT-5 Mini · OpenAI account") but they are independent choices.
    # SET NULL so deleting an LLM connector doesn't block — the agent simply has no brain
    # until the user picks another one.
    model_connector_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("connectors.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    model: str = Field(default="")
    status: AgentStatus = Field(default=AgentStatus.draft)
    settings: dict[str, Any] = Field(
        default_factory=lambda: dict(DEFAULT_AGENT_SETTINGS),
        sa_type=JSONB,
    )
    # Snapshot of the whole agent config taken at publish time. The runtime reads only from
    # here, so editing the draft never changes what is running.
    published_config: dict[str, Any] | None = Field(default=None, sa_type=JSONB)
    published_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    # Debounce run-failure notifications: only notify after NOTIFY_FAILURE_DEBOUNCE_H hours.
    last_failure_notified_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = _ts()
    updated_at: datetime = _ts()


# ── AgentTool ──────────────────────────────────────────────────────────────────

class AgentTool(SQLModel, table=True):
    """Binds a connector to an agent. The available tools derive from the connector's type;
    `enabled_tools` narrows that set when null/empty means "all tools for this type"."""

    __tablename__ = "agent_tools"
    __table_args__ = (UniqueConstraint("agent_id", "connector_id", name="uq_agent_connector"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    connector_id: UUID = Field(foreign_key="connectors.id", index=True)
    # Disambiguates two connectors of the same type on one agent, e.g. send_email_sales
    # vs send_email_support. Empty when the agent has only one connector of this type.
    alias: str = Field(default="")
    enabled_tools: list[str] | None = Field(default=None, sa_type=JSONB)
    # Tools whose calls must be approved before execution. Stored unaliased (base names).
    approval_tools: list[str] | None = Field(default=None, sa_type=JSONB)
    created_at: datetime = _ts()


# ── AgentTrigger ───────────────────────────────────────────────────────────────

class AgentTrigger(SQLModel, table=True):
    """How an agent gets woken up. `config` holds a cron expression for schedules or a
    connector_id for channels."""

    __tablename__ = "agent_triggers"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    type: TriggerType
    config: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    enabled: bool = Field(default=True)
    last_run_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    next_run_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), index=True
    )
    created_at: datetime = _ts()


# ── AgentSession ───────────────────────────────────────────────────────────────

class AgentSession(SQLModel, table=True):
    __tablename__ = "agent_sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    trigger_type: TriggerType
    status: SessionStatus = Field(default=SessionStatus.running)
    # Generated from the first turn once the agent has done something worth naming.
    name: str = Field(default="")
    # Simulated run: write tools return a preview instead of performing the action.
    dry_run: bool = Field(default=False)
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)
    iterations: int = Field(default=0)
    error: str | None = Field(default=None)
    # Set when this run was started by another agent calling the agent-as-tool.
    # SET NULL on delete so session pruning never blocks.
    triggered_by_session_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_sessions.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    started_at: datetime = _ts()
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ── AgentSessionMessage ────────────────────────────────────────────────────────

class AgentSessionMessage(SQLModel, table=True):
    """One turn in a session. Written as the loop runs, not at the end, so a crashed run
    still leaves a readable trace."""

    __tablename__ = "agent_session_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="agent_sessions.id", index=True)
    # Monotonic within a session — created_at alone can collide on fast loops.
    sequence: int = Field(default=0)
    role: MessageRole
    content: str = Field(default="")
    tool_name: str | None = Field(default=None)
    tool_args: dict[str, Any] | None = Field(default=None, sa_type=JSONB)
    created_at: datetime = _ts()


# ── AgentProcessedItem ─────────────────────────────────────────────────────────

class ProcessedItemStatus(str, Enum):
    in_flight = "in_flight"
    """Reserved by an active session. Other runs skip this item while it is in_flight."""
    permanent = "permanent"
    """Confirmed by a completed session. Permanently excluded from future runs."""


class AgentProcessedItem(SQLModel, table=True):
    """Idempotency ledger with two-phase reservation.

    When an agent reads an item (email, Telegram message, SMS) the tool immediately
    writes an `in_flight` row.  Other runs see it and skip it.  When the session that
    read the item succeeds, the row is upgraded to `permanent`.  If the session fails,
    is rejected, or times out, the worker deletes the row so the next run can try again.

    This makes the ledger correct across approval pauses, worker crashes, and any future
    multi-stage workflows — not just the simple "run succeeds on first try" case.
    """

    __tablename__ = "agent_processed_items"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "connector_id", "external_id", name="uq_agent_processed_item"
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    # SET NULL not CASCADE: the idempotency ledger outlives connector rotation. If you
    # replace a Gmail account, the old processed-item rows must survive so the agent does
    # not re-reply to emails it already handled when the new connector starts polling.
    connector_id: UUID | None = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("connectors.id", ondelete="SET NULL"),
            index=True,
            nullable=True,
        )
    )
    # Provider-side id: Gmail message id, Telegram update id, Twilio call sid.
    external_id: str = Field(index=True)
    # Which run handled it — provenance only. Nulled rather than cascaded when sessions are
    # pruned, because the ledger has to outlive them: losing a row here means re-replying to
    # an email months later.
    session_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    status: ProcessedItemStatus = Field(default=ProcessedItemStatus.permanent)
    processed_at: datetime = _ts()


# ── InboundEvent ───────────────────────────────────────────────────────────────

class InboundEventStatus(str, Enum):
    pending = "pending"
    processed = "processed"
    failed = "failed"
    # No agent listens to this connector. Recorded rather than dropped so a user wiring up a
    # webhook can see their messages arriving before any agent exists to handle them.
    ignored = "ignored"


class InboundEvent(SQLModel, table=True):
    """A message pushed to us by Telegram or Twilio, waiting for the worker to act on it.

    Webhooks persist and return 200 immediately instead of running the agent inline: providers
    retry anything slow, and an agent run takes far longer than their patience. The unique
    constraint on the provider's own id is what makes those retries harmless — a redelivered
    update is the same row, not a second run.
    """

    __tablename__ = "inbound_events"
    __table_args__ = (
        UniqueConstraint("connector_id", "external_id", name="uq_inbound_event"),
        # Partial index: the worker only ever queries for pending rows, and processed ones
        # accumulate forever. Indexing the whole column would grow without bound to serve a
        # working set that stays near zero.
        Index(
            "ix_inbound_events_queue",
            "received_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # CASCADE: pending events for a deleted connector are undeliverable and should be cleaned
    # up automatically rather than blocking the delete with a FK violation.
    connector_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("connectors.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    # Telegram update_id, Twilio MessageSid.
    external_id: str = Field(index=True)
    # What the agent is given as its opening message.
    text: str = ""
    sender: str = ""
    # Full provider payload, kept for debugging and for tools that need more than the text.
    payload: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    status: InboundEventStatus = Field(default=InboundEventStatus.pending)
    error: str | None = None
    received_at: datetime = _ts()
    processed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


# ── Knowledge ──────────────────────────────────────────────────────────────────

# text-embedding-3-small. A platform-wide constant, not a setting: vectors from different
# models are not comparable, so changing this means re-embedding every chunk in the database.
EMBEDDING_DIMENSIONS = 1536


class KnowledgeFileStatus(str, Enum):
    pending = "pending"        # uploaded, waiting for the worker
    processing = "processing"  # worker is chunking and embedding it
    ready = "ready"            # searchable
    error = "error"            # parse or embedding failed; see .error


class AgentKnowledgeFile(SQLModel, table=True):
    """A document uploaded to one agent's knowledge base.

    The extracted text is kept on the row (not the original bytes): it is what chunking
    actually consumes, so files can be re-chunked or re-embedded later without re-upload,
    while the raw PDF would only ever be parsed once and then dead weight.
    """

    __tablename__ = "agent_knowledge_files"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    filename: str
    size_bytes: int = Field(default=0)
    status: KnowledgeFileStatus = Field(default=KnowledgeFileStatus.pending)
    error: str | None = Field(default=None)
    chunk_count: int = Field(default=0)
    text: str = Field(default="")
    created_at: datetime = _ts()


class AgentKnowledgeChunk(SQLModel, table=True):
    """One embedded slice of a knowledge file, what `search_knowledge` actually retrieves."""

    __tablename__ = "agent_knowledge_chunks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # CASCADE: chunks are derived data; they never outlive their file.
    file_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_knowledge_files.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    seq: int = Field(default=0)
    content: str
    embedding: Any = Field(sa_column=Column(Vector(EMBEDDING_DIMENSIONS)))


# ── Approvals ──────────────────────────────────────────────────────────────────

class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"   # auto-rejected by the worker after expires_at


class ApprovalRequest(SQLModel, table=True):
    """One tool call that requires human sign-off before the agent may proceed.

    The run is suspended (session.status = waiting_approval) until the workspace owner
    approves or rejects via the Approvals page.  The worker then resumes the session by
    re-entering the reasoning loop at the exact point it paused.

    `messages_snapshot` is the full OpenAI-style messages list at the moment of pause,
    serialised to JSON.  Storing it avoids the need to reconstruct it from AgentSession-
    Messages on resume, which would require tool_call_id round-trips the DB does not keep.

    `pending_tool_calls` is every tool call the model requested in the same turn, including
    non-approval ones.  On resume the non-approval ones are executed first, then the gated
    one receives the approval result.
    """

    __tablename__ = "approval_requests"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # CASCADE: a deleted session can never be resumed, so its pending approvals are junk.
    session_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)

    # The specific tool call awaiting sign-off.
    tool_call_id: str   # LLM-generated ID, needed to build the tool result on resume
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    # One-line plain-English description shown on the approval card.
    summary: str = Field(default="")

    # All tool calls the model requested in this turn, as [{id, name, args}].
    # Required to reconstruct the assistant message on resume.
    pending_tool_calls: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSONB)
    # Full messages list at point of pause, for resumption without re-reading the DB.
    messages_snapshot: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSONB)

    status: ApprovalStatus = Field(default=ApprovalStatus.pending)
    # Optional reason from the reviewer, handed back to the model on rejection.
    response_note: str | None = Field(default=None)

    created_at: datetime = _ts()
    resolved_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    # The worker auto-rejects anything older than this.
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))


# ── Publish snapshots ──────────────────────────────────────────────────────────

class AgentPublishSnapshot(SQLModel, table=True):
    """One entry per publish event — the frozen config at that moment.

    Kept indefinitely so owners can review what changed across versions and
    restore any past config back to the draft (rollback).  The snapshot payload
    mirrors what `snapshot_config()` produces: instructions, model,
    model_connector_id, and settings.
    """

    __tablename__ = "agent_publish_snapshots"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agents.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    published_by: UUID = Field(foreign_key="users.id")
    # Full snapshot: {instructions, model, model_connector_id, settings}
    config: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    # Version number within this agent (1 = first publish, 2 = second, …)
    version: int = Field(default=1)
    published_at: datetime = _ts()


# ── Assist threads ─────────────────────────────────────────────────────────────

class AgentAssistMessage(SQLModel, table=True):
    """One turn in the persistent prompt-assistant conversation for an agent.

    There is exactly one thread per agent (identified by agent_id).  Messages are
    stored in insertion order; the frontend loads them all and the backend sends the
    full history on every chat request so the model has memory across sessions.
    """

    __tablename__ = "agent_assist_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agents.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    role: str  # "user" | "assistant"
    content: str = Field(sa_type=JSONB)  # stored as text, JSONB handles large strings fine
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)
    created_at: datetime = _ts()

# ── Skills ────────────────────────────────────────────────────────────────────

class Skill(SQLModel, table=True):
    """A reusable prompt fragment owned by an org.

    Default skills are seeded on org creation (is_default=True) and are fully
    editable.  Users can also create their own.  Skills are attached to agents
    individually via AgentSkillLink.
    """

    __tablename__ = "skills"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    # Stable key from DEFAULT_SKILLS; None for user-created skills.
    key: str | None = Field(default=None)
    name: str
    tagline: str = Field(default="")
    category: str = Field(default="Custom")   # Behaviour | Output | Safety | Domain | Custom
    content: str = Field(sa_type=JSONB)
    is_default: bool = Field(default=False)   # seeded from DEFAULT_SKILLS
    created_at: datetime = _ts()
    updated_at: datetime = _ts()


class AgentSkillLink(SQLModel, table=True):
    """Many-to-many join: which skills are active on which agent."""

    __tablename__ = "agent_skill_links"

    agent_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    skill_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("skills.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    attached_at: datetime = _ts()


# ── Owner Notes ────────────────────────────────────────────────────────────────

class OwnerNote(SQLModel, table=True):
    """Short owner-authored text injected verbatim into agent system prompts.

    Two kinds, distinguished by `agent_resolvable`:
    - Fact  — expires by date or never. Agents read but never write.
    - Task  — expires when an agent marks it done via `mark_note_done`. Useful
              for waitlists, callbacks, one-off reminders. The resolve is a
              claim-safe conditional update so two concurrent agents cannot
              both act on the same task.

    Notes bypass the publish snapshot deliberately — they take effect on the
    next run without a republish.
    """

    __tablename__ = "owner_notes"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    created_by: UUID = Field(foreign_key="users.id")
    body: str  # ≤500 chars, enforced at the route
    # Which agents see this note. None = all agents in the org.
    # Mirrors the null-means-all convention from AgentTool.enabled_tools.
    agent_ids: list[str] | None = Field(default=None, sa_type=JSONB)
    expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    # Task semantics: owner opts in, agents can then mark it done.
    agent_resolvable: bool = Field(default=False)
    resolved_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    resolved_by: UUID | None = Field(default=None)  # agent that claimed it
    resolution: str = Field(default="")  # agent's one-line note, owner-facing only
    created_at: datetime = _ts()
    updated_at: datetime = _ts()


# ── AgentLink ─────────────────────────────────────────────────────────────────

class AgentLink(SQLModel, table=True):
    """Grants one agent (caller) the ability to invoke another (target) as a tool.

    The description is the owner's "when to use" line — it becomes the tool's
    description verbatim, so the calling model can route correctly. Only published
    targets produce a live tool; unpublished targets are silently skipped at build time.
    """

    __tablename__ = "agent_links"
    __table_args__ = (UniqueConstraint("agent_id", "target_agent_id", name="uq_agent_link"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agents.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    target_agent_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    # Shown to the model as the tool description and to the owner in the UI.
    description: str
    created_at: datetime = _ts()


# ── AgentScenario ─────────────────────────────────────────────────────────────

class AgentScenario(SQLModel, table=True):
    """A named test case the owner can run against their agent in dry-run mode.

    The runner injects `input_text` as the trigger message, executes the agent
    with dry_run=True, and records the resulting AgentSession for inspection.
    Optional `expected_tools` are checked by the CI scenario harness.
    """

    __tablename__ = "agent_scenarios"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agents.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    name: str  # e.g. "Happy path: new lead"
    input_text: str  # sample trigger message injected on dry-run
    # Optional ordered list of tool names CI asserts were called, e.g. ["send_email"]
    expected_tools: list[str] = Field(default=[], sa_column=Column(JSONB, nullable=False, server_default="'[]'"))
    # Last dry-run session id (None if never run)
    last_session_id: UUID | None = Field(default=None)
    last_ran_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_at: datetime = _ts()
