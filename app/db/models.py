import secrets
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Index, LargeBinary, String, UniqueConstraint, text
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
    instagram = "instagram"
    hubspot = "hubspot"
    pipedrive = "pipedrive"
    notion = "notion"
    airtable = "airtable"
    shopify = "shopify"
    google_business_profile = "google_business_profile"
    calendly = "calendly"
    openai = "openai"
    anthropic = "anthropic"
    mcp = "mcp"
    tables = "tables"  # built-in: org-level typed tables, auto-provisioned per org


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
    agent = "agent"   # run started by another agent calling this one as a tool
    phone = "phone"   # inbound voice call via Twilio ConversationRelay


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
    is_staff: bool = Field(default=False)
    # Incremented on logout so old tokens are immediately rejected.
    # No Redis needed — one extra DB column is sufficient for our traffic.
    token_version: int = Field(default=0, nullable=False)
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
    # Stripe customer ID — stored here so it survives subscription row deletions.
    stripe_customer_id: str | None = Field(default=None, index=True)
    # Data retention policy. None = keep forever.
    # Sessions older than this many days are pruned by the nightly worker.
    data_retention_days: int | None = Field(default=None)
    # When True, prune only deletes message content (PII scrub) rather than the whole session row.
    # The session header (status, token counts, name, timestamps) is kept for cost reporting.
    scrub_content_only: bool = Field(default=False)
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


# ── Admin audit log ───────────────────────────────────────────────────────────

class AdminAuditLog(SQLModel, table=True):
    """Immutable audit trail for staff actions taken in the /admin panel."""

    __tablename__ = "admin_audit_log"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    staff_user_id: UUID = Field(foreign_key="users.id", index=True)
    action: str  # e.g. "update_is_staff", "update_connector_status", "create_org_override"
    target_type: str | None = None  # "user" | "organization" | "connector" | …
    target_id: str | None = None  # stringified UUID of the affected row
    meta: dict | None = Field(default=None, sa_type=JSONB)
    created_at: datetime = _ts()


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
    # Key-value memory tools (memory_get/set/delete/list). On by default: "the last id I
    # processed" is the core need of nearly every scheduled agent.
    "kv_memory": True,
    "daily_token_budget": 500_000,
    # Media processing policy for inbound channel messages.
    # "auto"  = process before the agent runs (Whisper / vision / extract)
    # "skip"  = store the file, show a marker in the transcript, never call the API
    # Default is skip for all kinds — cost is zero until explicitly opted in.
    # Documents use knowledge.extract_text (no API call), so they default to auto.
    "media_policy": {
        "audio": "skip",
        "image": "skip",
        "video": "skip",
        "document": "auto",
    },
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
    `enabled_tools` narrows that set: null means "all tools for this type", a list is the
    exact allow-list (an empty list means none — the default for the built-in tables connector)."""

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
    # Model identifier used for this run, e.g. "gpt-4o" or "claude-sonnet-4-5".
    # Stored so the cost breakdown can use the correct price per token.
    model_slug: str = Field(default="")
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
    # Set for channel-triggered runs; links the session to the conversation thread
    conversation_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )

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


class AgentCursor(SQLModel, table=True):
    """Per-agent read position on a provider stream.

    Read tools discover new items by *position* (Telegram message id per chat, IMAP UID per
    folder), never by the provider's unread flag.  The unread flag is shared state: the
    owner reading a group on their phone, or a second agent on the same account, would
    otherwise hide messages from this agent.  With a cursor each agent has its own view.

    The cursor is advisory (a fetch lower bound).  `AgentProcessedItem` remains the
    correctness ledger — it is what stops an item from being surfaced twice.  The cursor is
    advanced only when the run succeeds, so a failed run re-reads the same window and the
    ledger de-duplicates whatever was already confirmed.
    """

    __tablename__ = "agent_cursors"
    __table_args__ = (
        UniqueConstraint("agent_id", "connector_id", "scope", name="uq_agent_cursor"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), index=True
        )
    )
    connector_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True), ForeignKey("connectors.id", ondelete="CASCADE"), index=True
        )
    )
    # Telegram: chat id.  Gmail: folder name ("INBOX").
    scope: str
    # Opaque string: Telegram message id, IMAP UID.  Compared numerically by the tool.
    cursor: str
    updated_at: datetime = _ts()


# ── InboundEvent ───────────────────────────────────────────────────────────────

# ── Conversation enums ────────────────────────────────────────────────────────

class ConversationStatus(str, Enum):
    open = "open"       # agent is handling replies
    human = "human"     # owner has taken over; agent pauses for this thread
    closed = "closed"   # thread archived


class MessageDirection(str, Enum):
    inbound = "inbound"
    outbound = "outbound"


class MessageAuthor(str, Enum):
    peer = "peer"       # the external person
    agent = "agent"     # the AI agent
    human = "human"     # the workspace owner replying manually


class MessageKind(str, Enum):
    text = "text"
    image = "image"
    audio = "audio"
    video = "video"
    document = "document"
    location = "location"
    sticker = "sticker"
    contact = "contact"
    other = "other"


# ── Conversation tables ───────────────────────────────────────────────────────

class Conversation(SQLModel, table=True):
    """One ongoing thread between the agent and a specific external person on one channel.

    Keyed on (connector_id, peer_id, thread_key):
    - Instagram DM: peer_id = sender IG user id, thread_key = ""
    - Instagram comment: peer_id = commenter IG user id, thread_key = media_id (one thread
      per commenter per post so replies don't bleed across posts)
    - WhatsApp / Twilio: peer_id = E.164 phone number
    - Telegram: peer_id = chat.id (numeric string) — for groups this is the group id and
      `is_group` is set; the individual speaker lives on each ConversationMessage
    """

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("connector_id", "peer_id", "thread_key", name="uq_conversation"),
        Index("ix_conversations_connector_last", "connector_id", "last_inbound_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    connector_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("connectors.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    # ConnectorType value string ("telegram_bot", "whatsapp", etc.)
    channel: str = Field(index=True)
    # Provider-native peer identifier (chat id, wa_id, IG user id, phone number)
    peer_id: str
    # Human-readable name if available (username, display name)
    peer_name: str = Field(default="")
    # Discriminator for Instagram comments (media id); empty for all other channels
    thread_key: str = Field(default="")
    # True for Telegram groups/supergroups/channels: many speakers, peer_name = group title
    is_group: bool = Field(default=False)
    status: ConversationStatus = Field(default=ConversationStatus.open)
    # Rolling LLM-compressed summary of older turns (updated after each run)
    summary: str = Field(default="")
    # Messages up to and including this timestamp are folded into summary
    summary_through_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_inbound_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_outbound_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = _ts()


class ConversationMessage(SQLModel, table=True):
    """One turn in a conversation — either inbound from the peer or outbound from the agent/human."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conv_messages_conv_time", "conversation_id", "created_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    conversation_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("conversations.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    direction: MessageDirection
    author: MessageAuthor
    kind: MessageKind = Field(default=MessageKind.text)
    # Plain text body, or transcript / description once media has been processed
    text: str = Field(default="")
    # JSONB list of {kind, provider_ref, mime, filename, size, duration, caption,
    # stored_path, status: pending|ready|failed|unavailable, error, cost_usd}
    attachments: list[dict[str, Any]] = Field(default_factory=list, sa_type=JSONB)
    # Provider message id (for dedup / linking back to InboundEvent)
    external_id: str = Field(default="")
    # Who wrote it, for group conversations (empty in 1:1 threads where the peer is implied)
    speaker_id: str = Field(default="")
    speaker_name: str = Field(default="")
    # Telegram reply threading: the parent's provider id and a short excerpt of its text.
    # The excerpt is stored because the parent may predate the bot joining / the agent's
    # cursor, so it cannot always be looked up in this table.
    reply_to_external_id: str = Field(default="")
    reply_to_text: str = Field(default="")
    # The AgentSession that produced or consumed this message (SET NULL on session delete)
    session_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_sessions.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    created_at: datetime = _ts()


# ── InboundEventStatus / InboundEvent ─────────────────────────────────────────

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
    # Set when this event has been linked to a Conversation and ConversationMessage
    conversation_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    conversation_message_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("conversation_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


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
    # Set when the document came from a URL rather than a file upload.
    source_url: str | None = Field(default=None)
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


class AgentDataTable(SQLModel, table=True):
    """One queryable table derived from a CSV or XLSX knowledge file (one row per sheet).

    The data is kept as Parquet bytes on the row rather than in a directory on the VPS: it
    keeps Postgres the only datastore, so backups and deletes stay one mechanism. A 10 MB
    CSV is typically well under a megabyte as Parquet. `columns` and `sample` are rendered
    into the `query_data` tool description so the model knows the schema before it writes
    SQL; storing them avoids re-parsing the Parquet on every run.
    """

    __tablename__ = "agent_data_tables"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # CASCADE: derived from the file; never outlives it.
    file_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_knowledge_files.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    # SQL identifier the model uses: sanitised filename (+ sheet), unique per agent.
    name: str
    # Sheet name for XLSX; None for CSV.
    sheet: str | None = Field(default=None)
    row_count: int = Field(default=0)
    # [{"name": "ref_no", "type": "VARCHAR", "original": "Ref No"}, ...]
    columns: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    # Up to three rows as lists of strings, for the tool description.
    sample: list[list[str]] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    parquet: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    created_at: datetime = _ts()


class AgentKV(SQLModel, table=True):
    """One key-value entry an agent wrote for its future self (or for its workspace).

    `agent_id` NULL means workspace-shared: any agent in the org reads and writes it through
    the `shared:` key prefix. The uniqueness constraint is declared NULLS NOT DISTINCT so
    two shared rows cannot hold the same key — plain UNIQUE would treat the NULL agent_ids
    as distinct and the upsert's ON CONFLICT would never fire.

    Values are JSON (number, string, list, object) rather than text so a "last processed
    id" round-trips as the number the agent stored. Kept deliberately small (see
    `app.core.kv` limits): this is state, not a document store.
    """

    __tablename__ = "agent_kv"
    __table_args__ = (
        UniqueConstraint("org_id", "agent_id", "key", name="uq_agent_kv_scope_key", postgresql_nulls_not_distinct=True),
        Index("ix_agent_kv_org_agent", "org_id", "agent_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", nullable=False)
    # CASCADE: an agent's private state dies with it. Shared rows have no agent and survive.
    agent_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=True),
    )
    # Stored without the `shared:` prefix; the scope is the agent_id column.
    key: str
    value: Any = Field(sa_column=Column(JSONB, nullable=False))
    # Which run last wrote it — lets the Memory tab link "set by run …".
    updated_by_session_id: UUID | None = Field(default=None, sa_type=PGUUID(as_uuid=True))
    created_at: datetime = _ts()
    updated_at: datetime = _ts()


# ── Episodic memory ────────────────────────────────────────────────────────────

class AgentMemoryEntry(SQLModel, table=True):
    """One embedded memory note written by an agent at the end of a successful run.

    The agent extracts the ``MEMORY:`` line from its closing message; that text is
    embedded and stored here.  ``_recall`` searches this table by cosine similarity to
    the current trigger message, which lets the agent surface relevant past observations
    even after many intervening runs.
    """

    __tablename__ = "agent_memory_entries"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: UUID = Field(foreign_key="agents.id", index=True)
    session_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agent_sessions.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
            unique=True,  # one entry per session — ON CONFLICT DO NOTHING keeps upsert safe
        )
    )
    note: str                    # the MEMORY: line text (or closing message excerpt)
    embedding: Any = Field(sa_column=Column(Vector(EMBEDDING_DIMENSIONS)))
    created_at: datetime = _ts()


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


class CodeSkillDeployStatus(str, Enum):
    draft = "draft"
    deploying = "deploying"
    ready = "ready"
    failed = "failed"


class CodeSkill(SQLModel, table=True):
    """A user-authored Python function deployed as an AWS Lambda and exposed to agents as a tool.

    Distinct from ``Skill``, which is a prompt fragment. See CODE_SKILLS.md.
    """

    __tablename__ = "code_skills"
    __table_args__ = (UniqueConstraint("org_id", "tool_name", name="uq_code_skills_org_tool_name"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    created_by_id: UUID = Field(foreign_key="users.id")

    name: str
    tagline: str = Field(default="")
    tool_name: str
    tool_description: str
    input_schema: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))

    source: str
    source_sha256: str
    timeout_seconds: int = Field(default=10)
    network_access: bool = Field(default=False)
    read_only: bool = Field(default=False)
    secrets_enc: str | None = Field(default=None)

    deploy_status: str = Field(
        default=CodeSkillDeployStatus.draft.value,
        sa_column=Column(String(16), nullable=False, server_default="draft"),
    )
    deployed_sha256: str | None = Field(default=None)
    deployed_network_access: bool | None = Field(default=None)
    deployed_timeout_seconds: int | None = Field(default=None)
    lambda_function_name: str | None = Field(default=None)
    lambda_arn: str | None = Field(default=None)
    last_deploy_error: str | None = Field(default=None)
    last_deployed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))

    invocation_count: int = Field(default=0)
    last_invoked_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_error: str | None = Field(default=None)

    created_at: datetime = _ts()
    updated_at: datetime = _ts()

    @property
    def dirty(self) -> bool:
        """True when the live function differs from the saved row."""
        return (
            self.deployed_sha256 != self.source_sha256
            or self.deployed_network_access != self.network_access
            or self.deployed_timeout_seconds != self.timeout_seconds
        )


class AgentCodeSkillLink(SQLModel, table=True):
    """Which code skills an agent may call, and whether each call needs approval."""

    __tablename__ = "agent_code_skill_links"

    agent_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    code_skill_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("code_skills.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )
    requires_approval: bool = Field(default=False)
    attached_at: datetime = _ts()


class CodeSkillDeploy(SQLModel, table=True):
    """Audit log: one row per deploy attempt."""

    __tablename__ = "code_skill_deploys"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code_skill_id: UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("code_skills.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    requested_by_id: UUID = Field(foreign_key="users.id")
    source_sha256: str
    network_access: bool
    outcome: str = Field(default="pending")
    error: str | None = Field(default=None)
    started_at: datetime = _ts()
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


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


# ── Org Tables ────────────────────────────────────────────────────────────────
# Agent-native business data layer.  One OrgTable == one typed spreadsheet-like
# table owned by an organisation.  Rows are stored as JSONB and agents access
# them through generated tools (leads_search, leads_create, …) exposed via the
# built-in "tables" connector (ConnectorType.tables).


class OrgTable(SQLModel, table=True):
    """Schema definition for one org-level typed table.

    `columns` is a list of column descriptors:
      {
        "key":  "contact",           # slug used as JSONB key and in tool args
        "name": "Contact",           # display name shown in the grid
        "type": "phone",             # one of COLUMN_TYPES
        "options": ["new","done"],   # only for select
        "link_table_id": "<uuid>",   # only for link columns
        "required": false,
        "hidden_from_agents": false  # strip from tool descriptions + DuckDB loads
      }

    `unique_on` is a list of column keys that together form a dedup key.  When
    create_row finds a collision it returns the existing row id rather than
    inserting a duplicate (idempotent create semantics).
    """

    __tablename__ = "org_tables"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    name: str
    slug: str  # tool-safe; unique per org; e.g. "leads"
    description: str = Field(default="")
    columns: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="'[]'"))
    unique_on: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="'[]'"))
    created_by: UUID = Field(foreign_key="users.id")
    created_at: datetime = _ts()
    updated_at: datetime = _ts()
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))


class OrgTableRow(SQLModel, table=True):
    """One data row in an OrgTable.  All values stored as JSONB keyed by column key."""

    __tablename__ = "org_table_rows"
    __table_args__ = (
        Index("ix_org_table_rows_table_deleted", "table_id", "deleted_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    table_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("org_tables.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="'{}'"))
    version: int = Field(default=1)
    created_at: datetime = _ts()
    updated_at: datetime = _ts()
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_by_user_id: UUID | None = Field(default=None)
    created_by_session_id: UUID | None = Field(default=None)


class OrgTableEvent(SQLModel, table=True):
    """Audit log: every mutation of an OrgTable or its rows.

    `action` values: create, update, delete, restore, schema
    `before`/`after`: full row data snapshot (None for creates/schema).
    """

    __tablename__ = "org_table_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    table_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("org_tables.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    row_id: UUID | None = Field(default=None)
    action: str  # create | update | delete | restore | schema
    before: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    after: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    actor_user_id: UUID | None = Field(default=None)
    actor_session_id: UUID | None = Field(default=None)
    agent_id: UUID | None = Field(default=None)
    created_at: datetime = _ts()


# ═══════════════════════════════════════════════════════════════════════════════
# Billing / Plans / Entitlements
# ═══════════════════════════════════════════════════════════════════════════════

class Plan(SQLModel, table=True):
    """Product catalog: a named plan with a feature set and soft limits."""

    __tablename__ = "plans"

    code: str = Field(primary_key=True)  # 'free' | 'pro' | 'business'
    display_name: str
    price_usd_monthly: int = Field(default=0)   # USD cents
    price_usd_annual: int = Field(default=0)    # USD cents/year
    # Monthly managed-model credit included in the plan, in USD cents.
    # 0 for Free (no managed models), 1500 for Pro ($15), 6000 for Business ($60).
    monthly_credit_cents: int = Field(default=0)
    # Feature flags: e.g. {"managed_models": true, "voice": false}
    features: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="'{}'"))
    # Soft limits: {"agents": 5, "rows": 5000}  — no "members" limit
    limits: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="'{}'"))
    # Included meter quantities per billing period (legacy voice_minutes; model_credits now via credit_grants)
    included: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="'{}'"))
    stripe_monthly_price_id: str | None = Field(default=None)
    stripe_annual_price_id: str | None = Field(default=None)
    active: bool = Field(default=True)
    sort_order: int = Field(default=0)
    created_at: datetime = _ts()


class Addon(SQLModel, table=True):
    """Add-on product catalog: voice packs, row quota boosts, etc."""

    __tablename__ = "addons"

    code: str = Field(primary_key=True)  # 'voice_lite' | 'voice_standard' | 'rows_100k'
    display_name: str
    price_usd_monthly: int = Field(default=0)   # USD cents
    sort_order: int = Field(default=0)
    # Feature flags enabled by this add-on
    features: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="'{}'"))
    # Meter allocations: {"voice_minutes": 400}
    included: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="'{}'"))
    meter: str | None = Field(default=None)  # 'voice_minutes' | None
    stripe_price_id: str | None = Field(default=None)
    # USD metered overage price (e.g. per voice_minute beyond included)
    stripe_overage_price_id: str | None = Field(default=None)
    active: bool = Field(default=True)
    created_at: datetime = _ts()


class ModelPrice(SQLModel, table=True):
    """LLM pricing table (USD per million tokens).  Used for cost accounting and markup."""

    __tablename__ = "model_prices"
    __table_args__ = (
        UniqueConstraint("provider", "model_slug", "active_from", name="uq_model_prices_slot"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    provider: str                      # 'openai' | 'anthropic'
    model_slug: str                    # e.g. 'gpt-4.1', 'claude-sonnet-4-5'
    input_per_m: float = Field(default=0.0)   # USD per 1M input tokens
    output_per_m: float = Field(default=0.0)  # USD per 1M output tokens
    audio_in_per_m: float = Field(default=0.0)
    audio_out_per_m: float = Field(default=0.0)
    # markup multiplier applied when Setod provides the key (platform managed)
    managed_markup: float = Field(default=1.5)
    active_from: datetime = Field(sa_type=DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    active: bool = Field(default=True)


class OrgSubscription(SQLModel, table=True):
    """Active subscription for an org (at most one non-cancelled row per org)."""

    __tablename__ = "org_subscriptions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True, unique=True)
    plan_code: str = Field(foreign_key="plans.code")
    status: str = Field(default="active")  # 'active' | 'past_due' | 'cancelled'
    stripe_customer_id: str | None = Field(default=None, index=True)
    stripe_subscription_id: str | None = Field(default=None, unique=True)
    current_period_start: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    current_period_end: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    cancel_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    pending_plan_code: str | None = Field(default=None)  # downgrade scheduled: plan code at next renewal
    # Plan-included voice overage tracking. Add-on overage tracking remains on OrgAddon
    # for legacy purchases; new plan-based voice uses these fields.
    voice_overage_reported: float = Field(default=0.0)
    voice_overage_period_start: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = _ts()
    updated_at: datetime = _ts()


class OrgAddon(SQLModel, table=True):
    """Add-on purchases active for an org."""

    __tablename__ = "org_addons"
    __table_args__ = (
        UniqueConstraint("org_id", "addon_code", name="uq_org_addons_slot"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    addon_code: str = Field(foreign_key="addons.code")
    stripe_subscription_id: str | None = Field(default=None)
    stripe_subscription_item_id: str | None = Field(default=None)
    status: str = Field(default="active")  # 'active' | 'cancel_at_period_end' | 'cancelled'
    # Proration: allowance for the partial period in which the add-on was added
    activated_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    included_snapshot: float | None = Field(default=None)       # prorated minutes for current period
    snapshot_period_end: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    # Scheduled removal / swap-down
    cancel_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    pending_addon_code: str | None = Field(default=None)        # swap-down: code to activate at renewal
    # Overage reporting (incremental delta sent to Stripe)
    overage_reported: float = Field(default=0.0)
    overage_period_start: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = _ts()


class OrgOverride(SQLModel, table=True):
    """Manual grants / limit overrides applied by staff (highest priority)."""

    __tablename__ = "org_overrides"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    key: str           # e.g. 'voice' | 'agents' | 'voice_minutes_included'
    value: str         # JSON-serialized value: "true" | "1000" | …
    reason: str = Field(default="")
    expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_by: UUID | None = Field(default=None, foreign_key="users.id")
    created_at: datetime = _ts()


class OrgCap(SQLModel, table=True):
    """Hard caps that cannot be unlocked by any plan or override."""

    __tablename__ = "org_caps"
    __table_args__ = (
        UniqueConstraint("org_id", "meter", name="uq_org_caps_meter"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    meter: str    # 'voice_minutes' | 'model_credits'
    hard_cap: int
    created_at: datetime = _ts()


class UsageEvent(SQLModel, table=True):
    """One billing event (one LLM turn, one voice call minute block, etc.)."""

    __tablename__ = "usage_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    agent_id: UUID | None = Field(default=None)
    session_id: UUID | None = Field(default=None)
    meter: str            # 'model_credits' | 'voice_minutes'
    quantity: float = Field(default=0.0)
    cost_usd: float = Field(default=0.0)
    billable: bool = Field(default=True)    # False for BYOK runs
    idempotency_key: str = Field(unique=True, index=True)  # 'session:{id}:turn:{n}'
    meta: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    created_at: datetime = _ts()


class UsagePeriod(SQLModel, table=True):
    """Rolled-up totals per org per meter per calendar month."""

    __tablename__ = "usage_periods"
    __table_args__ = (
        UniqueConstraint("org_id", "period_start", "meter", name="uq_usage_periods_slot"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    period_start: datetime = Field(sa_type=DateTime(timezone=True))   # first day of month UTC
    meter: str
    included: float = Field(default=0.0)      # from plan/addons at snapshot time
    used: float = Field(default=0.0)
    overage: float = Field(default=0.0)
    pushed_to_stripe_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    updated_at: datetime = _ts()


class StripeEvent(SQLModel, table=True):
    """Idempotency store for processed Stripe webhook events."""

    __tablename__ = "stripe_events"

    event_id: str = Field(primary_key=True)   # evt_… from Stripe
    processed_at: datetime = _ts()


class CreditGrant(SQLModel, table=True):
    """Prepaid managed-model credit balance for an org.

    Sources:
      'plan'         — monthly grant from invoice.paid; expires at period_end.
      'purchase'     — top-up pack bought via Checkout; expires in 12 months.
      'auto_recharge'— off-session PaymentIntent recharge; expires in 12 months.
      'promo'        — manual staff grant; custom expiry.

    Draw order: oldest-expiring first; plan grants before purchased grants
    (see credits.py draw()).  remaining_cents decrements as runs consume credit;
    exhausted grants are kept for audit (remaining_cents == 0, not deleted).
    """

    __tablename__ = "credit_grants"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    amount_cents: int                          # original grant amount, never mutated
    remaining_cents: int                       # decremented by draw()
    source: str = Field(default="plan")        # 'plan' | 'purchase' | 'auto_recharge' | 'promo'
    stripe_ref: str | None = Field(default=None)  # invoice_id, pi_id, or checkout session_id
    granted_at: datetime = _ts()
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    created_at: datetime = _ts()


class OrgBillingSettings(SQLModel, table=True):
    """Per-org billing preferences and auto-recharge state."""

    __tablename__ = "org_billing_settings"

    org_id: UUID = Field(primary_key=True, foreign_key="organizations.id")
    # Auto-recharge: when balance drops below threshold_cents, charge amount_cents.
    auto_recharge_enabled: bool = Field(default=False)
    threshold_cents: int = Field(default=500)   # $5.00
    recharge_amount_cents: int = Field(default=2500)  # $25.00
    monthly_cap_cents: int = Field(default=20000)     # $200.00 safety ceiling per month
    # Running counter reset each calendar month (reset in worker).
    auto_recharged_this_month_cents: int = Field(default=0)
    # De-dup timestamps for notification emails.
    notified_50pct_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    notified_80pct_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    notified_low_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    notified_zero_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    auto_recharge_failed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    allow_voice_overage: bool = Field(default=True)
    updated_at: datetime = _ts()


# ── MCP personal access tokens ────────────────────────────────────────────────

class ApiTokenScope(str, Enum):
    read = "read"
    write = "write"


class ApiToken(SQLModel, table=True):
    """Personal access token for the MCP server. One token = one user + one workspace."""

    __tablename__ = "api_tokens"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    name: str = Field(max_length=80)
    token_hash: str = Field(unique=True, index=True, max_length=64)
    token_prefix: str = Field(max_length=8)
    # Plain text, matching the migration. A Postgres enum named apitokenscope was never created.
    scope: ApiTokenScope = Field(default=ApiTokenScope.read, sa_column=Column(String(8), nullable=False, server_default="read"))
    token_version_at_creation: int = Field(default=0)
    expires_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_used_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    revoked_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    created_at: datetime = _ts()


class McpAuditEvent(SQLModel, table=True):
    """One row per MCP write (and per setod_run_agent). Arguments are capped at insert time."""

    __tablename__ = "mcp_audit_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    token_id: UUID = Field(foreign_key="api_tokens.id", index=True)
    user_id: UUID = Field(foreign_key="users.id")
    org_id: UUID = Field(foreign_key="organizations.id", index=True)
    tool: str = Field(max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict, sa_type=JSONB)
    ok: bool = Field(default=True)
    error: str | None = Field(default=None)
    duration_ms: int = Field(default=0)
    created_at: datetime = _ts()
