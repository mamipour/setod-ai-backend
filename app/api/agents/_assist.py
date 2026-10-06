"""Assistant (copilot) sub-router for the agents API.

Extracted from router.py (R2 refactor). Mounted at no prefix; the parent
router (/agents) provides the path prefix so routes remain identical.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.agents._helpers import get_owned_agent
from app.api.auth.dependencies import get_current_user
from app.core import kv
from app.core.agents.base import RegisteredTool
from app.core.crypto import decrypt_json
from app.core.llm.client import (
    ToolSpec,
    build_client,
    assistant_message,
    system_message,
    tool_message,
)
from app.db.models import (
    Agent,
    AgentAssistMessage,
    AgentLink,
    AgentSession,
    AgentSessionMessage,
    AgentSkillLink,
    AgentTool,
    Connector,
    ConnectorType,
    DEFAULT_AGENT_SETTINGS,
    MessageRole,
    Organization,
    Skill,
    User,
)
from app.db.session import get_session
from app.integrations import websearch as _websearch
from app.docs.guide import load_guide
from app.limiter import limiter

log = logging.getLogger("setod.assist")
assist_router = APIRouter()

# ── Assistant ─────────────────────────────────────────────────────────────────

_COPILOT_PREAMBLE = """You are a prompt engineer embedded inside setod, an AI agent automation platform.

Your only job is to help the user write, improve, and debug the instruction prompt for their agent.

## What you have access to

Every request includes a `<agent_context>` block injected into the system with the latest live data:
- The agent's **current instructions** (the full prompt it runs with)
- The **tools** currently attached (which connectors and which tool functions are enabled)
- The **skills** currently attached (reusable behaviour rules the agent runs alongside its instructions)
- The **last up to 20 run logs**, each showing: status (ok / error / waiting_approval), timestamp, trigger type, token count, and a one-line summary

Use this data proactively:
- If the user asks whether the agent is healthy, check the recent run statuses and summaries — tell them what you see (e.g. "Your last 3 runs all errored", "Looks healthy — 5 successful runs in the past week")
- If the user asks why something went wrong, look at the run statuses and summaries before asking them to share logs
- If the user asks to improve the prompt, read the current instructions first so your suggestions are grounded in what's already there
- If no tools are attached, proactively note that the agent can't take any actions yet
- If a skill in the org library covers what the user is describing but is not attached to this agent, point them to it: "There's a Silence when idle skill in your library — attach it from the Agent tab → Skills instead of writing that rule into the prompt"
"""

_COPILOT_RULES = """## Research tools (you can use these yourself)

You have three tools available in this conversation:

- `search_web` — search the web and get titles, URLs, and short snippets. Use it whenever you need to look something up to give a grounded answer.
- `fetch_page` — fetch the full text of any public URL. Use it to read a page and understand its structure before writing a prompt that references it.
- `read_run_trace` — read one of this agent's past runs step by step: every tool it called, the arguments it passed, what each tool returned, and its closing message. Runs are numbered in the context below, 1 being the most recent.

Use these tools proactively when the user gives you a URL or asks about a third-party service you are not certain about. Do not invent API formats, RSS URLs, or field names — verify them.

Call at most 4–6 tools per reply. Stop as soon as you have enough information to write the prompt.

## Your job

When the user describes what they want their agent to do:
1. If the goal is genuinely ambiguous, ask ONE clarifying question and stop — do not also write a prompt in the same reply. Wait for the answer.
2. If you can make a reasonable assumption, state it and write the prompt — do not ask a question.
3. Present the prompt in a fenced code block; the chat renders a Copy button on it.

When the user shares an existing prompt and asks for improvements:
1. Identify the specific problem (vague trigger, no silence rule, missing tool constraint, etc.)
2. Rewrite the prompt with the fix applied
3. Explain in one sentence what changed and why

When the user shares run logs and asks why something went wrong:
1. Read the logs carefully
2. Identify the root cause
3. Suggest a specific prompt change that prevents it — quote the exact line to add or change

## Output rules

**Code block purity — this is strict**
- Put the ready-to-use prompt text inside the fenced code block and NOTHING ELSE.
- Do NOT put notes, caveats, admin comments, skill suggestions, follow-up questions, or "Notes for admin" sections inside the code block. A user will copy that block verbatim into their agent. Anything that should not be in the agent's instructions must go OUTSIDE the code block, after it.

**You cannot modify the agent**
- You have no ability to apply, save, or publish anything. You are a read-only advisor.
- Never say "apply this prompt", "I'll apply it", "want me to apply?", or any phrase implying you can make changes. The user copies your suggestion and pastes it themselves.
- Never offer numbered choices like "(1) apply as-is, (2) apply a variant" — you cannot apply either.

**Connector and tool honesty**
- Before suggesting any integration, check the agent's attached tools (shown in `<agent_context>`). If it's not attached, check whether it exists in the connector list above.
- Never suggest connecting a service that is not in the connector list above. Google Sheets and Notion have no agent tools. Airtable, HubSpot, Pipedrive, Shopify, Instagram, WhatsApp, Calendly, Google Business Profile, and Slack incoming webhooks do. Workspace tables and CSV knowledge are available. MCP is the path for a service with no connector, and only once that connector is attached.
- If you want to suggest a follow-on capability that would require a connector the user does not have, say exactly: "This would need [connector name] — that connector doesn't exist on setod yet. You could request it at support."

**Other rules**
- Never invent connector types, tool names, or platform features not listed above
- Keep prompts concise — under 400 words unless the task genuinely requires more
- If the user asks something unrelated to their agent's prompt, redirect them:
  "I can help with your agent's instructions — what would you like the agent to do?"
"""

_ASSIST_SYSTEM = _COPILOT_PREAMBLE + "\n\n" + load_guide("all") + "\n\n" + _COPILOT_RULES


# All tools each connector type can expose; used when enabled_tools is null (= all on).
_ALL_CONNECTOR_TOOLS: dict[str, list[str]] = {
    "gmail": [
        "read_unread_emails",
        "search_emails",
        "send_email",
        "reply_to_email",
        "archive_email",
        "list_calendar_events",
        "create_calendar_event",
    ],
    "telegram_bot": ["send_telegram_message"],
    "telegram_client": ["read_telegram_messages", "send_telegram_message"],
    "twilio": ["send_sms"],
    "slack_webhook": ["post_to_slack"],
    "whatsapp": ["send_whatsapp_message", "read_whatsapp_messages"],
    "instagram": [
        "get_instagram_posts",
        "get_instagram_comments",
        "reply_to_instagram_comment",
        "hide_instagram_comment",
        "delete_instagram_comment",
        "read_instagram_messages",
        "reply_to_instagram_dm",
    ],
    "hubspot": [
        "find_hubspot_contact",
        "create_hubspot_contact",
        "update_hubspot_contact",
        "create_hubspot_deal",
        "move_hubspot_deal",
        "log_hubspot_note",
        "list_hubspot_pipeline_stages",
    ],
    "pipedrive": [
        "find_pipedrive_person",
        "create_pipedrive_person",
        "update_pipedrive_person",
        "create_pipedrive_deal",
        "move_pipedrive_deal",
        "log_pipedrive_activity",
        "list_pipedrive_stages",
    ],
    "airtable": [
        "list_airtable_bases",
        "list_airtable_records",
        "find_airtable_record",
        "create_airtable_record",
        "update_airtable_record",
    ],
    "shopify": [
        "get_shopify_order",
        "list_shopify_orders",
        "search_shopify_customer",
        "list_shopify_products",
        "get_shopify_product",
        "add_shopify_order_note",
        "cancel_shopify_order",
    ],
    "google_business_profile": [
        "list_gbp_locations",
        "list_gbp_reviews",
        "reply_to_gbp_review",
        "delete_gbp_reply",
    ],
    "calendly": [
        "list_calendly_event_types",
        "get_calendly_availability",
        "list_calendly_events",
        "get_calendly_event",
        "create_calendly_booking",
        "cancel_calendly_event",
        "create_scheduling_link",
    ],
}


# Caps for a run trace handed to the copilot. Per-step so one giant tool result (a fetched
# CSV, a full inbox) cannot swallow the trace, and overall so a long run still fits in a
# reply. The middle is elided rather than the tail — the closing message and the last tool
# calls are usually what the question is about.
TRACE_STEP_CHARS = 1_200
TRACE_TOTAL_CHARS = 10_000


async def run_trace(session: AsyncSession, agent: Agent, run_number: int) -> str:
    """Render one past run's message trace for the copilot to read.

    Includes tool arguments, not just results: the body of an outbound message lives in
    `tool_args`, so without it the copilot cannot see what the agent actually sent.
    """
    runs_result = await session.exec(
        select(AgentSession)
        .where(AgentSession.agent_id == agent.id)
        .order_by(AgentSession.started_at.desc())
        .limit(20)
    )
    runs = runs_result.all()
    if not runs:
        return "This agent has no runs yet."
    if run_number < 1 or run_number > len(runs):
        return f"No run numbered {run_number}. This agent has {len(runs)} recent run(s), numbered 1 (most recent) to {len(runs)}."

    run = runs[run_number - 1]
    msgs_result = await session.exec(
        select(AgentSessionMessage)
        .where(AgentSessionMessage.session_id == run.id)
        .order_by(AgentSessionMessage.sequence)
    )
    messages = msgs_result.all()

    steps: list[str] = []
    for m in messages:
        body = (m.content or "").strip()
        if len(body) > TRACE_STEP_CHARS:
            body = body[:TRACE_STEP_CHARS] + " …[truncated]"
        if m.role == MessageRole.tool:
            args = json.dumps(m.tool_args or {}, ensure_ascii=False)
            if len(args) > TRACE_STEP_CHARS:
                args = args[:TRACE_STEP_CHARS] + " …[truncated]"
            steps.append(f"CALLED {m.tool_name}({args})\n  RETURNED: {body}")
        else:
            steps.append(f"{m.role.value.upper()}: {body}")

    # Keep the head and the tail, drop the middle if the whole thing will not fit.
    total = sum(len(s) for s in steps)
    if total > TRACE_TOTAL_CHARS:
        head, tail, budget = [], [], TRACE_TOTAL_CHARS // 2
        spent = 0
        for s in steps:
            if spent + len(s) > budget:
                break
            head.append(s); spent += len(s)
        spent = 0
        for s in reversed(steps[len(head):]):
            if spent + len(s) > budget:
                break
            tail.insert(0, s); spent += len(s)
        omitted = len(steps) - len(head) - len(tail)
        steps = head + ([f"…[{omitted} step(s) omitted]…"] if omitted > 0 else []) + tail

    started = run.started_at.strftime("%Y-%m-%d %H:%M UTC") if run.started_at else "?"
    header = (
        f"Run [{run_number}] — {run.status.value.upper()}, started {started}, "
        f"trigger {run.trigger_type.value if run.trigger_type else 'manual'}, "
        f"{run.total_tokens or 0} tokens"
        + (f", error: {run.error}" if run.error else "")
    )
    return header + "\n\n" + "\n\n".join(steps)


_run_trace = run_trace


async def _build_agent_context_block(session: AsyncSession, agent: Agent) -> str:
    """Build a live <agent_context> block for the system prompt on every request."""
    tool_rows = await session.exec(select(AgentTool).where(AgentTool.agent_id == agent.id))
    tool_lines = []
    for at in tool_rows.all():
        connector = await session.get(Connector, at.connector_id)
        if connector:
            # None means "all tools enabled"; a list is the exact allow-list (same as the runner)
            if connector.type == ConnectorType.mcp and connector.config:
                frozen = decrypt_json(connector.config).get("tools") or []
                catalog = [t.get("name") for t in frozen if t.get("name")]
            else:
                catalog = _ALL_CONNECTOR_TOOLS.get(connector.type.value, [])
            active = at.enabled_tools if at.enabled_tools is not None else catalog
            tool_lines.append(f"  - {connector.name or connector.type.value} ({', '.join(active)})")

    skills_result = await session.exec(
        select(Skill)
        .join(AgentSkillLink, AgentSkillLink.skill_id == Skill.id)
        .where(AgentSkillLink.agent_id == agent.id)
        .order_by(Skill.category, Skill.name)
    )
    skill_lines = [f"  - {s.name} ({s.category})" for s in skills_result.all()]

    calls_result = await session.exec(
        select(AgentLink, Agent)
        .join(Agent, Agent.id == AgentLink.target_agent_id)
        .where(AgentLink.agent_id == agent.id)
        .order_by(AgentLink.created_at)
    )
    call_lines = [
        f"  - {target.name} [{target.status.value}]: {link.description}"
        for link, target in calls_result.all()
    ]

    runs_result = await session.exec(
        select(AgentSession)
        .where(AgentSession.agent_id == agent.id)
        .order_by(AgentSession.started_at.desc())
        .limit(20)
    )
    run_lines = []
    # Numbered so the copilot can name one when calling read_run_trace. 1 = most recent.
    for n, r in enumerate(runs_result.all(), 1):
        date = r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "?"
        tokens = f" | {r.total_tokens} tok" if r.total_tokens else ""
        error = f" | error: {r.error[:80]}" if r.error else ""
        trigger = r.trigger_type.value if r.trigger_type else "manual"
        run_lines.append(f"  [{n}] [{r.status.value.upper()}] {date} ({trigger}){tokens}{error}")

    settings = {**DEFAULT_AGENT_SETTINGS, **(agent.settings or {})}
    web_on = settings.get("web_search", False)
    page_on = settings.get("live_page_access", False)
    web_status = (
        "Web search ON, live page access ON"
        if web_on and page_on
        else "Web search ON, live page access OFF"
        if web_on
        else "Web search OFF (both toggles off)"
    )
    memory_status = (
        f"Remember past runs (episodic) {'ON' if settings.get('episodic_memory') else 'OFF'}; "
        f"key-value memory tools {'ON' if settings.get('kv_memory', True) else 'OFF'}"
    )
    # Key names + previews only (decision #12): values are agent-written and must not be
    # able to steer the copilot by being quoted at it in full.
    kv_lines: list[str] = []
    if settings.get("kv_memory", True):
        kv_lines = [
            f"  - {kv.SHARED_PREFIX if e.agent_id is None else ''}{e.key} = {kv.preview(e.value)}"
            for e in (await kv.list_entries(session, agent.org_id, agent.id))[: kv.DESCRIPTION_MAX_KEYS]
        ]

    # Resolve the effective model label from the connector, since agent.model is often null
    # while model_connector_id points to the real provider. Show the connector name so the
    # copilot knows the agent is properly configured.
    model_label = agent.model or ""
    if not model_label and agent.model_connector_id:
        mc = await session.get(Connector, agent.model_connector_id)
        if mc:
            model_label = f"{mc.name} ({mc.type.value})"
    model_label = model_label or "not set — agent cannot run until a model connector is chosen"

    return (
        f"<agent_context>\n"
        f"Agent: {agent.name}\n"
        f"Model: {model_label}\n"
        f"Web settings: {web_status}\n"
        f"Memory settings: {memory_status}\n\n"
        f"Current instructions:\n```\n{agent.instructions or '(empty)'}\n```\n\n"
        f"Stored key-value memory (key = preview; the owner can edit these on the Memory tab):\n"
        f"{chr(10).join(kv_lines) or '  (nothing stored)'}\n\n"
        f"Attached tools:\n{chr(10).join(tool_lines) or '  (none)'}\n\n"
        f"Attached skills:\n{chr(10).join(skill_lines) or '  (none)'}\n\n"
        f"Agents it can call:\n{chr(10).join(call_lines) or '  (none)'}\n\n"
        f"Last {len(run_lines)} runs:\n{chr(10).join(run_lines) or '  (no runs yet)'}\n"
        f"</agent_context>"
    )


class AssistChatRequest(BaseModel):
    content: str
    model_connector_id: UUID


@assist_router.get("/{agent_id}/assist/messages")
async def list_assist_messages(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Return the full persistent conversation thread for this agent."""
    agent = await get_owned_agent(session, current_user, agent_id)
    result = await session.exec(
        select(AgentAssistMessage)
        .where(AgentAssistMessage.agent_id == agent.id)
        .order_by(AgentAssistMessage.created_at)
    )
    return [{"id": str(m.id), "role": m.role, "content": m.content} for m in result.all()]


@assist_router.post("/{agent_id}/assist/chat")
@limiter.limit("60/minute")
async def assist_chat(
    request: Request,
    agent_id: UUID,
    body: AssistChatRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Save the user message, stream the assistant response, then save the completed reply."""
    agent = await get_owned_agent(session, current_user, agent_id)

    connector = await session.get(Connector, body.model_connector_id)
    if not connector or connector.org_id != agent.org_id:
        raise HTTPException(status_code=404, detail="Model connector not found")

    config = decrypt_json(connector.config)
    api_key = config.get("api_key", "")
    # Pick the best reasoning-capable model from our copilot preference list.
    # Falls back to the agent's configured model when nothing from the list is available,
    # so the copilot always gets a valid model even on restricted API keys.
    configured_model = config.get("model") or agent.model or ""
    from app.core.llm.client import pick_copilot_model
    model = await pick_copilot_model(connector.type.value, api_key, fallback=configured_model)
    llm = build_client(connector.type.value, api_key, model)

    # Persist the user message immediately
    user_msg = AgentAssistMessage(agent_id=agent.id, role="user", content=body.content)
    session.add(user_msg)
    await session.commit()

    # Load full thread history (including the message we just saved)
    history_result = await session.exec(
        select(AgentAssistMessage)
        .where(AgentAssistMessage.agent_id == agent.id)
        .order_by(AgentAssistMessage.created_at)
    )
    history = history_result.all()

    # Build the fresh context block and inject it into the system prompt.
    # NOTE: REASONING_PREAMBLE is intentionally NOT used here. The models we
    # select (gpt-5-mini, claude-sonnet-5, etc.) reason natively and silently.
    # Adding the preamble caused the model to output its internal monologue as
    # visible reply text ("Reasoning: I reviewed..."), which clutters the chat.
    context_block = await _build_agent_context_block(session, agent)
    full_system = _ASSIST_SYSTEM + "\n\n" + context_block

    msgs: list[dict] = [system_message(full_system)]
    for m in history:
        msgs.append({"role": m.role, "content": m.content})

    # Research tools — always available to the copilot, no connector required.
    from app.core.workspace import get_tavily_key
    _copilot_org = await session.get(Organization, agent.org_id)
    _research_tools = _websearch.build_tools(
        live_page_access=True,
        context_size="medium",
        tavily_api_key=get_tavily_key(_copilot_org),
    )

    # Lets the copilot read what an agent actually did on a past run: the tools it called,
    # the arguments it passed (which is where an outbound message body lives), and what
    # came back. Without it the copilot only sees run status and has to guess at causes.
    async def _trace_handler(args: dict, dry_run: bool) -> str:
        try:
            run_number = int(args.get("run", 1))
        except (TypeError, ValueError):
            return "Error: 'run' must be a whole number, 1 for the most recent run."
        return await _run_trace(session, agent, run_number)

    _research_tools = _research_tools + [
        RegisteredTool(
            spec=ToolSpec(
                name="read_run_trace",
                description=(
                    "Read the full step-by-step trace of one of this agent's past runs: "
                    "every tool it called, the arguments it passed, what each tool "
                    "returned, and its closing message. Use this before diagnosing why a "
                    "run behaved a certain way — do not guess from the run status alone. "
                    "Runs are numbered in the agent context, 1 being the most recent."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "run": {
                            "type": "integer",
                            "description": "Which run to read. 1 is the most recent.",
                        }
                    },
                    "required": ["run"],
                },
            ),
            handler=_trace_handler,
        )
    ]

    _research_specs = [t.spec for t in _research_tools]
    _research_handlers = {t.spec.name: t.handler for t in _research_tools}

    # Status label shown to the user while the model calls tools.
    def _status_line(tool_name: str, args: dict) -> str:
        if tool_name == "search_web":
            return f"Searching the web for \"{args.get('query', '')}\"…"
        if tool_name == "fetch_page":
            url = args.get("url", "")
            host = url.split("/")[2] if url.count("/") >= 2 else url
            return f"Reading {host}…"
        if tool_name == "read_run_trace":
            return f"Reading the trace of run {args.get('run', 1)}…"
        return f"Running {tool_name}…"

    async def event_stream():
        answer = ""
        total_prompt_tokens = 0
        total_completion_tokens = 0
        agent_tag = f"agent={agent_id} model={model!r}"
        try:
            # Bounded tool loop: up to RESEARCH_MAX_ROUNDS rounds.  If the model
            # still wants tools after the cap, one final call without tools forces
            # a text answer.
            RESEARCH_MAX_ROUNDS = 6
            loop_msgs = list(msgs)  # shallow copy so original is unchanged

            log.info("[assist] START %s user=%r", agent_tag, body.content[:120])

            for round_num in range(RESEARCH_MAX_ROUNDS + 1):
                force_text = round_num == RESEARCH_MAX_ROUNDS
                log.info("[assist] round=%d force_text=%s %s", round_num, force_text, agent_tag)

                resp = await llm.chat(
                    loop_msgs,
                    tools=None if force_text else _research_specs,
                )
                total_prompt_tokens += resp.prompt_tokens
                total_completion_tokens += resp.completion_tokens

                log.info(
                    "[assist] round=%d tool_calls=%d content_len=%d %s",
                    round_num,
                    len(resp.tool_calls or []),
                    len(resp.content or ""),
                    agent_tag,
                )

                if not resp.tool_calls or force_text:
                    # Final text answer — emit as a single streamed payload.
                    answer = resp.content
                    log.info("[assist] FINAL answer_len=%d %s", len(answer), agent_tag)
                    encoded = answer.replace("\n", "\\n")
                    yield f"data: {encoded}\n\n"
                    break

                # Execute each tool call and stream a status line for each.
                loop_msgs.append(assistant_message(resp.content, resp.tool_calls))
                for tc in resp.tool_calls:
                    log.info(
                        "[assist] tool_call name=%r args=%r %s",
                        tc.name,
                        tc.arguments,
                        agent_tag,
                    )
                    status = _status_line(tc.name, tc.arguments)
                    yield f"data: [STATUS] {status}\n\n"
                    handler = _research_handlers.get(tc.name)
                    if handler is None:
                        result = f"Unknown tool: {tc.name}"
                        log.warning("[assist] unknown tool %r %s", tc.name, agent_tag)
                    else:
                        result = await handler(tc.arguments, False)
                    log.info(
                        "[assist] tool_result name=%r result_len=%d snippet=%r %s",
                        tc.name,
                        len(result),
                        result[:200],
                        agent_tag,
                    )
                    loop_msgs.append(tool_message(tc.id, tc.name, result))

        except Exception as exc:
            log.exception("[assist] ERROR %s", agent_tag)
            yield f"data: [ERROR] {exc}\n\n"
        finally:
            # Persist the final answer text and the accumulated token counts.
            if answer:
                assistant_msg = AgentAssistMessage(
                    agent_id=agent.id,
                    role="assistant",
                    content=answer,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                )
                session.add(assistant_msg)
                await session.commit()
            log.info("[assist] DONE persisted=%s %s", bool(answer), agent_tag)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@assist_router.delete("/{agent_id}/assist/messages", status_code=204)
async def clear_assist_thread(
    agent_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Wipe the conversation thread for this agent."""
    agent = await get_owned_agent(session, current_user, agent_id)
    await session.exec(
        delete(AgentAssistMessage).where(AgentAssistMessage.agent_id == agent.id)
    )
    await session.commit()


