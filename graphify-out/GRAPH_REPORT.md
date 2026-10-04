# Graph Report - /home/farhad/projects/OIBC/platform  (2026-10-04)

## Corpus Check
- 196 files · ~137,061 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2218 nodes · 8523 edges · 147 communities (141 shown, 6 thin omitted)
- Extraction: 79% EXTRACTED · 21% INFERRED · 0% AMBIGUOUS · INFERRED: 1803 edges (avg confidence: 0.53)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Agents API Router
- Auth Dependencies & Connectors
- Billing Router
- Tabular Data Processor
- Table Schema Layer
- Table Service CRUD
- Admin Panel
- LLM Client & Crypto
- Admin Auth & JWT
- Workspace Management
- Agent Templates
- Key-Value Store
- Agent Runtime Base
- Connector Schemas
- Tables API Router
- Conversations & Messaging
- Gmail Integration
- Auth & Invite Router
- Notification System
- Integration Registry & Tests
- Module Group 20
- Module Group 21
- Module Group 22
- Module Group 23
- Module Group 24
- Module Group 25
- Module Group 26
- Module Group 27
- Module Group 28
- Module Group 29
- Module Group 30
- Module Group 31
- Module Group 32
- Module Group 33
- Module Group 34
- Module Group 35
- Module Group 36
- Module Group 37
- Module Group 38
- Module Group 39
- Module Group 40
- Module Group 41
- Module Group 42
- Module Group 43
- Module Group 44
- Module Group 45
- Module Group 46
- Module Group 47
- Module Group 48
- Module Group 49
- Module Group 50
- Module Group 51
- Module Group 52
- Module Group 53
- Module Group 54
- Module Group 55
- Module Group 56
- Module Group 57
- Module Group 58
- Module Group 59
- Module Group 60
- Module Group 61
- Module Group 62
- Module Group 63
- Module Group 64
- Module Group 65
- Module Group 66
- Module Group 67
- Module Group 68
- Module Group 69
- Module Group 70
- Module Group 110
- Module Group 111
- Module Group 112

## God Nodes (most connected - your core abstractions)
1. `User` - 289 edges
2. `get_session()` - 180 edges
3. `Connector` - 151 edges
4. `get_current_user()` - 150 edges
5. `Agent` - 134 edges
6. `ConnectorType` - 121 edges
7. `OrganizationMember` - 110 edges
8. `RegisteredTool` - 85 edges
9. `ToolSpec` - 82 edges
10. `TriggerType` - 81 edges

## Surprising Connections (you probably didn't know these)
- `TestBuildOpening` --uses--> `Peer`  [INFERRED]
  tests/unit/test_conversations.py → app/core/conversations.py
- `TestExtractAttachments` --uses--> `Peer`  [INFERRED]
  tests/unit/test_conversations.py → app/core/conversations.py
- `TestResolvePeer` --uses--> `Peer`  [INFERRED]
  tests/unit/test_conversations.py → app/core/conversations.py
- `TestDisplayName` --uses--> `Peer`  [INFERRED]
  tests/unit/test_groups_cursors.py → app/core/conversations.py
- `TestFormatBatches` --uses--> `Peer`  [INFERRED]
  tests/unit/test_groups_cursors.py → app/core/conversations.py

## Import Cycles
- 3-file cycle: `app/core/agents/base.py -> app/core/agents/calls.py -> app/integrations/base.py -> app/core/agents/base.py`

## Communities (147 total, 6 thin omitted)

### Community 0 - "Agents API Router"
Cohesion: 0.05
Nodes (175): add_knowledge_url(), _agent_tool_out(), _assert_model_connector(), _assert_org_member(), assist_chat(), AssistChatRequest, attach_agent_call(), attach_agent_tool() (+167 more)

### Community 1 - "Auth Dependencies & Connectors"
Cohesion: 0.09
Nodes (89): assert_org_owner(), get_current_user(), AsyncSession, Cookie, Raise 403 unless *user* is an owner of *org_id*. Use this inside route handlers…, _assert_org_member(), _check_llm_key(), _cleanup_tg_pending() (+81 more)

### Community 2 - "Billing Router"
Cohesion: 0.08
Nodes (73): alias, Depends, Dependency that enforces the current user is an *owner* of the workspace.…, require_owner(), _add_addon_line_item(), addon_preview(), _build_phase2_items(), change_plan() (+65 more)

### Community 3 - "Tabular Data Processor"
Cohesion: 0.06
Nodes (65): build_tool(), _cache_file(), _cell_to_csv(), _dedupe(), _detect_header_row(), _draft_from_csv_path(), _drafts_from_xlsx(), _ensure_cached() (+57 more)

### Community 4 - "Table Schema Layer"
Cohesion: 0.06
Nodes (60): _cap(), coerce_value(), derive_slug(), Any, ValueError, Column type registry for org tables =====================================…, Validate and normalise a column descriptor dict. Returns the normalised dict.…, Coerce and validate a single cell value against its column definition.… (+52 more)

### Community 5 - "Table Service CRUD"
Cohesion: 0.11
Nodes (56): add_column(), agents_using_table(), _check_unique_keys(), _check_unique_on(), create_row(), create_table(), delete_row(), delete_table() (+48 more)

### Community 6 - "Admin Panel"
Cohesion: 0.22
Nodes (53): StaffAuthBackend, sqladmin admin panel, mounted at /admin. Only accessible to is_staff users., AddonAdmin, AdminAuditLogAdmin, AgentAdmin, AgentSessionAdmin, ConnectorAdmin, ConversationAdmin (+45 more)

### Community 7 - "LLM Client & Crypto"
Cohesion: 0.09
Nodes (46): RegisteredTool, Record an outbound message from the agent (or human) into the conversation…, record_outbound(), decrypt_json(), Decrypt base64 ciphertext → JSON → dict., A tool as offered to the model. `parameters` is a JSON Schema object., ToolSpec, Everything about a table except its bytes — what a run needs before any query. (+38 more)

### Community 8 - "Admin Auth & JWT"
Cohesion: 0.07
Nodes (44): Admin, Custom sqladmin authentication backend using the existing JWT cookie., create_admin(), FastAPI, create_access_token(), decode_access_token(), UUID, accept_invitation_by_token() (+36 more)

### Community 9 - "Workspace Management"
Cohesion: 0.13
Nodes (49): change_member_role(), create_workspace(), get_notify_settings(), _get_org_as_member(), get_retention(), get_timezone(), get_web_search(), invite_member() (+41 more)

### Community 10 - "Agent Templates"
Cohesion: 0.11
Nodes (27): After Hours Responder — answers messages that arrive when nobody is working., Appointment Reminder — texts people the day before, so they turn up., Any, Agent templates =============== A template is an instruction sheet plus the…, Template, Canadian Tender Sniper — monitors CanadaBuys for new construction tenders., Emergency Email Triage — after-hours monitor that alerts on genuinely urgent…, Daily Inbox Digest — summarises every new email into a morning briefing. (+19 more)

### Community 11 - "Key-Value Store"
Cohesion: 0.09
Nodes (46): build_tools(), clear_private(), delete_entry(), encode(), get_entry(), KVError, _labelled(), list_entries() (+38 more)

### Community 12 - "Agent Runtime Base"
Cohesion: 0.10
Nodes (46): _build_client_for(), _describe_tool_call(), _embed(), _finish(), _name_session(), _pause_for_approval(), Any, AsyncSession (+38 more)

### Community 13 - "Connector Schemas"
Cohesion: 0.19
Nodes (41): AirtableCreate, CalendlyCreate, CredentialPatch, GmailConnectorBody, HubSpotCreate, LLMConnectorCreate, LLMKeyUpdate, LLMValidateBody (+33 more)

### Community 14 - "Tables API Router"
Cohesion: 0.22
Nodes (41): add_column(), _assert_member(), create_row(), create_table(), delete_row(), delete_table(), _event_out(), export_csv() (+33 more)

### Community 15 - "Conversations & Messaging"
Cohesion: 0.11
Nodes (36): _att_type_to_kind(), Attachment, get_or_create_conversation(), _instagram_attachments(), _mime_to_kind(), Peer, Any, AsyncSession (+28 more)

### Community 16 - "Gmail Integration"
Cohesion: 0.09
Nodes (39): archive_message(), _archive_sync(), _cfg(), _decode_header_str(), _extract_body(), fetch_messages(), _fetch_messages_sync(), get_message() (+31 more)

### Community 17 - "Auth & Invite Router"
Cohesion: 0.24
Nodes (34): AcceptInviteBody, BaseModel, CreateOrgBody, GraphEdge, GraphNode, InvitationOut, InviteBody, MemberOut (+26 more)

### Community 18 - "Notification System"
Cohesion: 0.13
Nodes (33): _link(), notify_approval_pending(), notify_budget_reached(), notify_connector_revoked(), notify_run_failed(), _owner(), AsyncSession, UUID (+25 more)

### Community 19 - "Integration Registry & Tests"
Cohesion: 0.09
Nodes (31): Tool names must match ^[a-zA-Z0-9_-]+$ for both providers., slug(), _base_name(), `send_email_sales` back to `send_email`, so enabled_tools can be stored…, _base_name(), _is_gated(), parametrize, Approval gate — unit tests. Tests the logic that determines which tool calls… (+23 more)

### Community 20 - "Module Group 20"
Cohesion: 0.13
Nodes (31): _external_id_from(), _get_connector(), AsyncSession, Depends, get, Header, post, Request (+23 more)

### Community 21 - "Module Group 21"
Cohesion: 0.12
Nodes (31): describe(), InvalidSchedule, next_run_after(), datetime, ValueError, Schedule triggers ================= Cron expressions, and the two decisions…, A human-readable summary for the builder and the triggers list., The cron expression or timezone cannot be used. Message is safe to show a user. (+23 more)

### Community 22 - "Module Group 22"
Cohesion: 0.13
Nodes (30): _begin_mcp_oauth(), authorize_url(), call_remote_tool(), discover_oauth(), exchange_code(), _extract_www_authenticate_metadata(), _host_is_blocked(), _is_slack_oauth() (+22 more)

### Community 23 - "Module Group 23"
Cohesion: 0.10
Nodes (21): _fernet(), Fernet symmetric encryption for storing connector credentials. Credentials are…, Provider shim ============= One interface over OpenAI and Anthropic so the…, AgentCursor, ProcessedItemStatus, Per-agent read position on a provider stream. Read tools discover new items by…, _headers(), Airtable integration ==================== Auth: Personal Access Token (PAT) —… (+13 more)

### Community 24 - "Module Group 24"
Cohesion: 0.10
Nodes (30): _describe_image(), _effective_media_policy(), _extract_document(), _failure_marker(), _fetch_bytes(), _fetch_instagram_file(), _fetch_telegram_file(), _fetch_twilio_file() (+22 more)

### Community 25 - "Module Group 25"
Cohesion: 0.10
Nodes (30): _build_fetch_and_query_csv_handler(), build_tools(), _clean(), _csv_schema_text(), _ddg_search(), _dom_to_text(), _download_csv_bytes(), _fetch_handler() (+22 more)

### Community 26 - "Module Group 26"
Cohesion: 0.10
Nodes (25): IntegrationError, RuntimeError, A tool could not do its job. Surfaced to the model as the tool result., _get_account_id(), _headers(), Google Business Profile integration ===================================== Auth:…, Resolve the first Google Business Profile account id., test_connection() (+17 more)

### Community 27 - "Module Group 27"
Cohesion: 0.14
Nodes (25): build_tool(), chunk_text(), claim_pending_files(), embed_texts(), extract_text(), fetch_url_text(), index_file(), KnowledgeError (+17 more)

### Community 28 - "Module Group 28"
Cohesion: 0.14
Nodes (25): OrgBillingSettings, Per-org billing preferences and auto-recharge state., Delete in_flight rows whose session has ended without confirming them. Runs on…, release_abandoned_reservations(), _check_credit_thresholds(), _claim_resolved_approvals(), _expire_approvals(), _index_knowledge() (+17 more)

### Community 29 - "Module Group 29"
Cohesion: 0.11
Nodes (22): MonkeyPatch, FakeLLMClient, _find_spec(), _minimal_args(), Any, Fake LLM client for scenario testing. The fake drives the agent loop by…, Scripted client that emits one tool call per turn until the list is exhausted.…, Build the smallest valid argument dict from a JSON Schema. (+14 more)

### Community 30 - "Module Group 30"
Cohesion: 0.23
Nodes (24): ApprovalDecision, ApprovalRequestOut, approve(), _assert_org_member(), _enrich(), _get_owned_request(), _get_owner_request(), list_approvals() (+16 more)

### Community 31 - "Module Group 31"
Cohesion: 0.22
Nodes (24): _assert_org_member(), create_note(), delete_note(), _get_note(), list_notes(), NoteCreate, NoteOut, NoteUpdate (+16 more)

### Community 32 - "Module Group 32"
Cohesion: 0.17
Nodes (23): attach_voice_number(), detach_voice_number(), _get_trigger(), _get_twilio_creds(), AsyncSession, post, Request, UUID (+15 more)

### Community 33 - "Module Group 33"
Cohesion: 0.20
Nodes (23): _connector_of_type(), AsyncSession, BaseModel, Depends, get, Header, post, Request (+15 more)

### Community 34 - "Module Group 34"
Cohesion: 0.19
Nodes (14): Drop UIDs at or below the cursor. `UID n:*` is inclusive of the highest UID…, _uids_after(), ChatBatch, format_batches(), New messages in one dialog since the agent's cursor, oldest first., Reza (@reza) #123' — name for reading, id for the lead alert. No id twice., Render fetched chats for the model. Pure function — covered by unit tests., One message as the read tool sees it (provider-agnostic, testable). (+6 more)

### Community 35 - "Module Group 35"
Cohesion: 0.18
Nodes (22): _do_auto_recharge(), Attempt an off-session PaymentIntent charge for the auto-recharge amount.…, balance(), draw(), grant(), grant_plan_credit(), grant_topup(), _maybe_auto_recharge() (+14 more)

### Community 36 - "Module Group 36"
Cohesion: 0.30
Nodes (21): _assert_org_member(), attach_skill(), create_skill(), delete_skill(), detach_skill(), _get_owned_agent(), _get_skill(), list_agent_skills() (+13 more)

### Community 37 - "Module Group 37"
Cohesion: 0.16
Nodes (21): _dedupe_keys(), _duck_type_to_ours(), _parse_csv_bytes(), _parse_csv_rows(), parse_rows(), _parse_xlsx_bytes(), _parse_xlsx_rows(), preview_import() (+13 more)

### Community 38 - "Module Group 38"
Cohesion: 0.19
Nodes (21): claim_due(), claim_inbound(), fire_channel_triggers(), is_running(), AsyncSession, datetime, UUID, Turning triggers into runs ========================== Claiming due work,… (+13 more)

### Community 39 - "Module Group 39"
Cohesion: 0.15
Nodes (14): AnthropicClient, build_client(), LLMError, LLMResponse, _loads(), OpenAIClient, Any, RuntimeError (+6 more)

### Community 40 - "Module Group 40"
Cohesion: 0.20
Nodes (20): prune_sessions(), Delete or scrub sessions older than each org's retention policy. Returns a dict…, _make_db(), _org(), asyncio, Unit tests for the data retention / PII scrub logic in prune_sessions. All…, Scrub mode: counts how many session IDs were found for scrubbing., An org with data_retention_days=None is skipped even if it has old sessions. (+12 more)

### Community 41 - "Module Group 41"
Cohesion: 0.14
Nodes (18): get_org_usage(), _get_price(), has_price(), push_voice_overage_to_stripe(), Any, AsyncSession, datetime, UUID (+10 more)

### Community 42 - "Module Group 42"
Cohesion: 0.18
Nodes (17): delete(), delete_conversation(), _ext_for_mime(), get(), _media_root(), _path(), put(), UUID (+9 more)

### Community 43 - "Module Group 43"
Cohesion: 0.21
Nodes (11): _build_opening(), Compose a single opening message from one or more inbound events in a bundle., InboundEvent, A message pushed to us by Telegram or Twilio, waiting for the worker to act on…, _event(), asyncio, UUID, Unit tests for the conversation layer. Tests are fully isolated from the… (+3 more)

### Community 44 - "Module Group 44"
Cohesion: 0.22
Nodes (16): _client(), create_event(), _create_event_sync(), _event_to_dict(), list_events(), _list_events_sync(), _primary_calendar(), _primary_tz_sync() (+8 more)

### Community 45 - "Module Group 45"
Cohesion: 0.23
Nodes (16): bot_call(), bot_send(), client_new_messages(), client_send(), _media_marker(), _msg_text(), mtproto_client(), Any (+8 more)

### Community 46 - "Module Group 46"
Cohesion: 0.33
Nodes (15): _assert_org_access(), get_conversation(), list_conversations(), patch_conversation(), AsyncSession, Depends, get, patch (+7 more)

### Community 47 - "Module Group 47"
Cohesion: 0.24
Nodes (15): ColumnOut, ColumnPatch, EventOut, BaseModel, RowCreate, RowListOut, RowPatch, TableCreate (+7 more)

### Community 48 - "Module Group 48"
Cohesion: 0.17
Nodes (15): build_tools(), AsyncSession, UUID, Return one call_X tool for every published agent linked to the caller. Returns…, build_tool(), live_notes(), prompt_block(), AsyncSession (+7 more)

### Community 49 - "Module Group 49"
Cohesion: 0.30
Nodes (9): format_turn(), message_body(), Text plus media markers for one message, as the agent should read it., One transcript line. 1:1 thread:: ``Reza [2026-09-26 14:02]: text`` Group…, ConversationMessage, One turn in a conversation — either inbound from the peer or outbound from the…, _conv(), _msg() (+1 more)

### Community 50 - "Module Group 50"
Cohesion: 0.22
Nodes (4): Extract peer identity from a raw provider payload. Called before writing…, resolve_peer(), TestResolvePeer, TestResolvePeerGroups

### Community 51 - "Module Group 51"
Cohesion: 0.14
Nodes (5): Shared parent domain for the auth cookie in production. Setting…, Pre-registered confidential client for a catalog MCP server, if we have one., Origins the browser may call from. `localhost` and `127.0.0.1` are the same…, Settings, BaseSettings

### Community 52 - "Module Group 52"
Cohesion: 0.23
Nodes (12): What `POST /agents/{id}/publish` freezes into `published_config`., snapshot_config(), AgentSessionMessage, One turn in a session. Written as the loop runs, not at the end, so a crashed…, main(), Slice 1 smoke test — drives the runtime directly, no HTTP. Creates a throwaway…, show(), check() (+4 more)

### Community 53 - "Module Group 53"
Cohesion: 0.22
Nodes (13): AsyncSession, UUID, Build an Entitlements object for the org from the DB., resolve(), AsyncSession, UUID, Voice billing helpers: period usage lookup and call-gating logic., Return the total voice minutes used in the current Stripe billing period. (+5 more)

### Community 54 - "Module Group 54"
Cohesion: 0.21
Nodes (13): _archive_non_usd_prices(), _ensure_billing_meter(), _ensure_metered_price(), _ensure_recurring_price(), _find_existing_usd_price(), _find_or_create_product(), main(), Idempotent script: create USD Stripe prices for all plans and add-ons, archive… (+5 more)

### Community 55 - "Module Group 55"
Cohesion: 0.21
Nodes (5): Entitlements, Return True if the feature is enabled (override > plan/addon)., Return numeric limit; -1 means unlimited., Raise if ``current`` has already reached the limit (-1 = unlimited)., Raise if the org is over quota for this meter.

### Community 56 - "Module Group 56"
Cohesion: 0.29
Nodes (3): extract_attachments(), Extract the text body and any media attachments from a provider payload.…, TestExtractAttachments

### Community 57 - "Module Group 57"
Cohesion: 0.22
Nodes (10): _creds(), Any, Google Sheets integration ========================= Uses a platform-level…, Accept a spreadsheet URL or a bare ID., Parse the JSON and return the service-account email., _rows_to_text(), _service(), _sheet_id() (+2 more)

### Community 58 - "Module Group 58"
Cohesion: 0.31
Nodes (10): build_tools_for_agent(), _fetch_org_tables(), _flush_cursors(), flush_seen(), AsyncSession, UUID, Fetch non-deleted org tables for the tables connector tool builder., Write the items surfaced by read tools to the idempotency ledger. Two modes… (+2 more)

### Community 59 - "Module Group 59"
Cohesion: 0.28
Nodes (8): do_run_migrations(), Run migrations in 'offline' mode. This configures the context with just a URL…, In this scenario we need to create an Engine and associate a connection with…, Run migrations in 'online' mode., run_async_migrations(), run_migrations_offline(), run_migrations_online(), Connection

### Community 60 - "Module Group 60"
Cohesion: 0.25
Nodes (8): _fmt_time(), _get_user_uri(), _headers(), Calendly integration ==================== Auth: Personal Access Token (PAT).…, Extract UUID from a Calendly resource URI like .../event_types/XXXXX., Convert UTC ISO string to readable format: 'Mon Sep 29 at 2:00 PM UTC'., Returns (user_uri, name, email)., _uuid_from_uri()

### Community 61 - "Module Group 61"
Cohesion: 0.31
Nodes (8): _headers(), _page_title(), _prop_to_text(), Notion integration ================== Auth: Internal connection token —…, Flatten Notion rich text array to plain string., Validate the token by searching with empty query. Returns workspace name., _rich_to_text(), test_connection()

### Community 62 - "Module Group 62"
Cohesion: 0.31
Nodes (8): parametrize, Daily token budget — unit tests. The budget check is embedded in run_agent();…, True when the run pushes accumulated spend over the cap. Mirrors the check in…, Two agents each get their own allowance — they never share a counter., test_budget_decision(), test_budget_is_per_agent_not_shared(), test_no_budget_never_triggers(), _would_exceed()

### Community 63 - "Module Group 63"
Cohesion: 0.36
Nodes (5): main(), Any, run_scenario(), ScenarioResult, TurnResult

### Community 64 - "Module Group 64"
Cohesion: 0.29
Nodes (3): Request, Send the user to the main app's Google login page., Called on every admin request. Returns None if authenticated, redirect…

### Community 65 - "Module Group 65"
Cohesion: 0.43
Nodes (3): display_name(), Best human label for a Telegram user: 'First Last (@user)' → '@user' → 'ID:123'., TestDisplayName

### Community 66 - "Module Group 66"
Cohesion: 0.60
Nodes (3): _ctx(), asyncio, TestToolContextCursors

### Community 70 - "Module Group 70"
Cohesion: 0.50
Nodes (4): _formula_escape(), Prefix cells that would be interpreted as formulas by spreadsheet apps., Values starting with = + - @ must be prefixed with ' to block injection., test_export_formula_escape()

## Knowledge Gaps
- **2 isolated node(s):** `DefaultSkill`, `run.sh script`
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Workspace Management` to `Agents API Router`, `Auth Dependencies & Connectors`, `Billing Router`, `Admin Panel`, `Admin Auth & JWT`, `Agent Templates`, `Connector Schemas`, `Tables API Router`, `Auth & Invite Router`, `Notification System`, `Module Group 28`, `Module Group 30`, `Module Group 31`, `Module Group 33`, `Module Group 36`, `Module Group 46`, `Module Group 47`, `Module Group 48`, `Module Group 52`?**
  _High betweenness centrality (0.119) - this node is a cross-community bridge._
- **Why does `Connector` connect `Connector Schemas` to `Agents API Router`, `Auth Dependencies & Connectors`, `Table Service CRUD`, `Admin Panel`, `LLM Client & Crypto`, `Admin Auth & JWT`, `Agent Templates`, `Agent Runtime Base`, `Conversations & Messaging`, `Gmail Integration`, `Auth & Invite Router`, `Notification System`, `Module Group 20`, `Module Group 23`, `Module Group 24`, `Module Group 26`, `Module Group 27`, `Module Group 29`, `Module Group 32`, `Module Group 33`, `Module Group 34`, `Module Group 38`, `Module Group 44`, `Module Group 45`, `Module Group 46`, `Module Group 52`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Why does `get_session()` connect `Workspace Management` to `Agents API Router`, `Auth Dependencies & Connectors`, `Billing Router`, `Module Group 32`, `Module Group 36`, `Module Group 33`, `Admin Auth & JWT`, `Module Group 46`, `Tables API Router`, `Auth & Invite Router`, `Module Group 20`, `Module Group 30`, `Module Group 31`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Are the 91 inferred relationships involving `User` (e.g. with `StaffAuthBackend` and `AddonAdmin`) actually correct?**
  _`User` has 91 INFERRED edges - model-reasoned connections that need verification._
- **Are the 85 inferred relationships involving `Connector` (e.g. with `AddonAdmin` and `AdminAuditLogAdmin`) actually correct?**
  _`Connector` has 85 INFERRED edges - model-reasoned connections that need verification._
- **Are the 144 inferred relationships involving `HTTPException` (e.g. with `add_knowledge_url()` and `_assert_model_connector()`) actually correct?**
  _`HTTPException` has 144 INFERRED edges - model-reasoned connections that need verification._
- **What connects `DefaultSkill`, `run.sh script` to the rest of the system?**
  _2 weakly-connected nodes found - possible documentation gaps or missing edges._