from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models import (
    AgentStatus,
    KnowledgeFileStatus,
    MessageRole,
    SessionStatus,
    TriggerType,
)


class AgentCreate(BaseModel):
    org_id: UUID
    # Blank means "take it from the template", which is what the create flow relies on when
    # the user accepts the suggested name.
    name: str = ""
    icon: str = ""
    instructions: str = ""
    template_key: str | None = None
    model_connector_id: UUID | None = None
    model: str = ""
    settings: dict[str, Any] | None = None


class AgentUpdate(BaseModel):
    """Every field optional — the builder autosaves one field at a time."""

    name: str | None = None
    icon: str | None = None
    instructions: str | None = None
    model_connector_id: UUID | None = None
    model: str | None = None
    settings: dict[str, Any] | None = None


class AgentOut(BaseModel):
    id: UUID
    org_id: UUID
    name: str
    icon: str
    instructions: str
    template_key: str | None
    model_connector_id: UUID | None
    model: str
    status: AgentStatus
    settings: dict[str, Any]
    has_unpublished_changes: bool = False
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Health: fraction of the last 20 non-dry-run sessions that succeeded.
    # None = no runs yet. Computed by the list/get endpoints, not stored.
    health_score: float | None = None
    # Timestamp of the most recent non-dry run. None = never run.
    last_run_at: datetime | None = None

    model_config = {"from_attributes": True}


class AgentToolAttach(BaseModel):
    connector_id: UUID
    # Left blank unless the agent has two connectors of the same type, where it becomes the
    # suffix that tells send_email_sales from send_email_support.
    alias: str = ""
    # Null means every tool the connector type offers.
    enabled_tools: list[str] | None = None
    # Tools that require human approval before execution (stored as base names, unaliased).
    approval_tools: list[str] | None = None


class ToolOut(BaseModel):
    """A single callable, as the builder's tool picker shows it."""

    name: str
    description: str
    enabled: bool
    requires_approval: bool = False


class AgentToolOut(BaseModel):
    id: UUID
    connector_id: UUID
    connector_name: str
    connector_type: str
    connector_status: str
    alias: str
    tools: list[ToolOut]


class TriggerUpsert(BaseModel):
    type: TriggerType
    # Schedules take {preset} or {cron, timezone}; channels take {connector_id}.
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class TriggerOut(BaseModel):
    id: UUID
    type: TriggerType
    config: dict[str, Any]
    enabled: bool
    # Plain-language rendering of config, so the UI does not have to parse cron itself.
    # Defaulted because it is derived by the router after validating the ORM row.
    summary: str = ""
    last_run_at: datetime | None
    next_run_at: datetime | None

    model_config = {"from_attributes": True}


class AgentRunRequest(BaseModel):
    org_id: UUID
    message: str | None = None
    # Defaults on: a user's first run should not touch their real inbox.
    # Real by default. Simulation stays available in the tool layer for callers that ask
    # for it explicitly, but nothing in the product currently does.
    dry_run: bool = False
    # Previews run the draft; production runs the published snapshot.
    use_draft: bool = False


class SessionMessageOut(BaseModel):
    id: UUID
    sequence: int
    role: MessageRole
    content: str
    tool_name: str | None
    tool_args: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SessionOut(BaseModel):
    id: UUID
    agent_id: UUID
    trigger_type: TriggerType
    status: SessionStatus
    name: str
    model_slug: str = ""
    dry_run: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int = 0
    iterations: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    # Set when this run was started by another agent.
    triggered_by_session_id: UUID | None = None
    triggered_by_agent_name: str | None = None  # resolved by the router

    model_config = {"from_attributes": True}


class AgentLinkOut(BaseModel):
    id: UUID
    agent_id: UUID
    target_agent_id: UUID
    target_agent_name: str
    target_agent_status: str
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentLinkCreate(BaseModel):
    target_agent_id: UUID
    description: str


class SessionDetailOut(SessionOut):
    messages: list[SessionMessageOut] = Field(default_factory=list)


class DataTableOut(BaseModel):
    """A queryable table derived from a CSV/XLSX file — metadata only, never the bytes."""

    name: str
    sheet: str | None = None
    row_count: int
    column_count: int


class KnowledgeFileOut(BaseModel):
    """One uploaded document, as the Knowledge tab lists it. The extracted text stays
    server-side - the UI only ever needs the filename and indexing state."""

    id: UUID
    filename: str
    size_bytes: int
    status: KnowledgeFileStatus
    error: str | None
    chunk_count: int
    source_url: str | None = None
    created_at: datetime
    # Non-empty for CSV/XLSX uploads the agent can query with `query_data`.
    tables: list[DataTableOut] = []

    model_config = {"from_attributes": True}
