"""
Agent runtime
=============
The reasoning loop: ask the model what to do, run whatever tools it asks for, feed the
results back, repeat until it stops asking or a limit is reached.

Two things are deliberate here.

Messages are written to the database as the loop runs rather than accumulated and flushed at
the end, so a run that crashes or is killed still leaves a readable trace of how far it got.
That costs a round trip per turn and is worth it — an agent that silently did nothing is the
hardest thing to debug for a non-technical user.

Every stop condition is recorded distinctly. "Finished", "hit the iteration ceiling", and
"ran out of budget" look identical from the outside if you only store success/failure, and
they need completely different fixes.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crypto import decrypt_json
from app.core.llm.client import (
    LLMError,
    ToolCall,
    ToolSpec,
    assistant_message,
    build_client,
    system_message,
    tool_message,
    user_message,
)
from app.db.models import (
    Agent,
    AgentSession,
    AgentSessionMessage,
    AgentSkillLink,
    ApprovalRequest,
    ApprovalStatus,
    Connector,
    ConnectorStatus,
    DEFAULT_AGENT_SETTINGS,
    MessageRole,
    ProcessedItemStatus,
    SessionStatus,
    Skill,
    TriggerType,
)

# Waiting sessions are auto-rejected after this period so they don't hang forever.
APPROVAL_TIMEOUT = timedelta(hours=24)

# A tool handler receives the model's parsed arguments and returns a string for the model.
# It is given `dry_run` so write actions can describe themselves instead of happening.
ToolHandler = Callable[[dict[str, Any], bool], Awaitable[str]]


@dataclass
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


class AgentRunError(RuntimeError):
    """Run could not start. Failures *during* a run are recorded on the session instead."""


# Scheduled runs have no human message to react to, so they get a neutral nudge. Anything
# more opinionated would compete with the agent's own instructions.
SCHEDULED_KICKOFF = "Run your scheduled task now."

# Wraps every agent run. Prevents hallucinated behaviour when the user's instructions are
# vague or empty — the agent must only do what it is *explicitly* told to do.
MASTER_PREAMBLE = (
    "You are an automated assistant. Follow the INSTRUCTIONS below exactly — do only what "
    "they describe, nothing more. If the instructions are empty, unclear, or do not apply "
    "to the data you see, reply with a short status summary and stop. Never invent tasks, "
    "never take actions that are not explicitly requested, and never reply to messages "
    "unless the instructions tell you to. When in doubt, do nothing."
)

# Without this, a model that receives "[dry run] would send the email" reads it as a failure
# and apologises to the user — which makes a perfectly working agent look broken during the
# one run that is supposed to build confidence in it.
DRY_RUN_PREAMBLE = (
    "This is a TEST RUN. Any tool you call will be simulated: it reports what it *would* "
    "have done instead of doing it, and its result begins with [simulated]. Treat every "
    "simulated result as a success. Report what you would have done, in the past "
    "conditional — do not apologise, and do not tell the user you lack access."
)

# Prefix on every simulated tool result, so the marker is consistent for the model and
# recognisable in the session trace.
DRY_RUN_PREFIX = "[simulated]"

# The `reasoning` setting. Kept as a prompt rather than a provider-specific parameter so it
# behaves the same on OpenAI and Anthropic, and on models that have no reasoning mode at all.
REASONING_PREAMBLE = (
    "Before each action, think it through: what the instructions ask for, what the data in "
    "front of you actually says, and which single next step follows. State that reasoning "
    "briefly, then act. Prefer being right over being fast."
)

# How many past runs `episodic_memory` carries forward, and how much of each. Enough to
# recognise a repeat situation, small enough not to crowd out the actual task.
MEMORY_RUNS = 5
MEMORY_CHARS = 400


async def run_agent(
    db: AsyncSession,
    agent: Agent,
    *,
    trigger_type: TriggerType = TriggerType.manual,
    user_input: str | None = None,
    tools: list[RegisteredTool] | None = None,
    dry_run: bool = False,
    use_published: bool = True,
    triggered_by_session_id: UUID | None = None,
) -> AgentSession:
    """Run an agent to completion and return its finished session.

    Reads `published_config` when `use_published` is set, so editing a draft never changes
    what production runs. Previews pass `use_published=False` to exercise the draft.

    Tools are resolved from the agent's connected accounts unless the caller passes an
    explicit list, which tests use to run against fakes.

    `triggered_by_session_id` links this run to the calling agent's session (agent-as-tool
    pattern). When set, no call_* tools are added — depth is capped at 1.
    """
    # Imported here because the integrations build on RegisteredTool from this module.
    from app.core import knowledge, notes
    from app.core.agents import calls
    from app.integrations import websearch
    from app.integrations.registry import build_tools_for_agent, confirm_seen, flush_seen

    config = _resolve_config(agent, use_published=use_published)
    settings = {**DEFAULT_AGENT_SETTINGS, **config.get("settings", {})}

    # Session is created first so its id is available to calls.build_tools (which embeds
    # it as triggered_by_session_id on any child sessions this run starts).
    session = AgentSession(
        agent_id=agent.id,
        org_id=agent.org_id,
        trigger_type=trigger_type,
        status=SessionStatus.running,
        dry_run=dry_run,
        triggered_by_session_id=triggered_by_session_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    contexts: list[Any] = []
    approval_required: set[str] = set()
    if tools is None:
        tools, contexts, approval_required = await build_tools_for_agent(db, agent.id)
        # Web tools come from settings, not from a connector, so they are appended here
        # rather than resolved from AgentTool rows.
        if settings["web_search"]:
            tools = tools + websearch.build_tools(
                live_page_access=settings["live_page_access"],
                context_size=settings["search_context"],
            )
        # Only offered when the agent actually has indexed documents.
        knowledge_tool = await knowledge.build_tool(db, agent.id)
        if knowledge_tool is not None:
            tools = tools + [knowledge_tool]
        # Only offered when there are open resolvable tasks in scope.
        notes_tool = await notes.build_tool(db, agent.org_id, agent.id)
        if notes_tool is not None:
            tools = tools + [notes_tool]
        # Depth-1 guard: only top-level runs (not themselves started by an agent) get
        # call_* tools. This prevents A→B→A deadlocks and unbounded chains.
        if triggered_by_session_id is None:
            call_tools = await calls.build_tools(db, agent, session.id)
            if call_tools:
                tools = tools + call_tools

    client = await _build_client_for(db, agent, config)

    opening = user_input or SCHEDULED_KICKOFF
    messages: list[dict[str, Any]] = []
    messages.append(system_message(MASTER_PREAMBLE))
    if config.get("instructions"):
        messages.append(system_message(f"INSTRUCTIONS:\n{config['instructions']}"))

    # Inject any skills the user has attached to this agent.
    skills_result = await db.exec(
        select(Skill)
        .join(AgentSkillLink, AgentSkillLink.skill_id == Skill.id)
        .where(AgentSkillLink.agent_id == agent.id)
        .order_by(Skill.category, Skill.name)
    )
    attached_skills = skills_result.all()
    if attached_skills:
        skill_block = "\n\n".join(s.content for s in attached_skills)
        messages.append(system_message(f"SKILLS (follow these rules throughout the run):\n\n{skill_block}"))

    # Owner notes: facts and resolvable tasks. Injected verbatim — they bypass
    # the publish snapshot so an owner can update them without republishing.
    owner_notes_list = await notes.live_notes(db, agent.org_id, agent.id)
    notes_block = notes.prompt_block(owner_notes_list)
    if notes_block:
        messages.append(system_message(notes_block))

    if settings["reasoning"]:
        messages.append(system_message(REASONING_PREAMBLE))
    if settings["episodic_memory"]:
        recalled = await _recall(db, agent.id, exclude=session.id)
        if recalled:
            messages.append(system_message(recalled))
    if dry_run:
        messages.append(system_message(DRY_RUN_PREAMBLE))
    messages.append(user_message(opening))

    seq = 0
    seq = await _record(db, session, seq, MessageRole.user, opening)

    budget = settings["daily_token_budget"]
    spent_before = await _tokens_used_today(db, agent.id)

    try:
        for iteration in range(settings["max_iterations"]):
            session.iterations = iteration + 1

            if budget and spent_before + session.total_tokens >= budget:
                await _finish(
                    db, session, SessionStatus.error,
                    error=f"Daily token budget of {budget:,} reached for this agent.",
                )
                return session

            response = await client.chat(
                messages,
                tools=[t.spec for t in tools] or None,
            )
            session.prompt_tokens += response.prompt_tokens
            session.completion_tokens += response.completion_tokens

            messages.append(assistant_message(response.content, response.tool_calls))
            if response.content:
                seq = await _record(db, session, seq, MessageRole.assistant, response.content)

            if not response.wants_tools:
                # A dry run must not affect what a later real run sees.
                if not dry_run:
                    await flush_seen(db, agent.id, contexts, session.id)
                await _finish(db, session, SessionStatus.succeeded)
                await _name_session(db, session, client, opening, response.content)
                return session

            # Check whether any tool in this batch requires human approval.
            # Dry runs skip approval - the point of a dry run is to see what would happen.
            if approval_required and not dry_run:
                gated = [tc for tc in response.tool_calls if tc.name in approval_required]
                if gated:
                    await _pause_for_approval(db, session, messages, response.tool_calls, gated[0], seq, contexts)
                    return session

            results = await _run_tools(
                db, session, response.tool_calls, tools,
                dry_run=dry_run, concurrency=settings["tool_concurrency"],
            )
            for call, output in results:
                seq = await _record(
                    db, session, seq, MessageRole.tool, output,
                    tool_name=call.name, tool_args=call.arguments,
                )
                messages.append(tool_message(call.id, call.name, output))

        await _finish(
            db, session, SessionStatus.error,
            error=(
                f"Stopped after {settings['max_iterations']} steps without finishing. "
                "Raise the step limit in Settings, or simplify the instructions."
            ),
        )
        return session

    except LLMError as exc:
        await _finish(db, session, SessionStatus.error, error=str(exc))
        return session
    except Exception as exc:  # noqa: BLE001 — a crashed run must still close its session
        await _finish(db, session, SessionStatus.error, error=f"{type(exc).__name__}: {exc}")
        return session


# ── Tool execution ─────────────────────────────────────────────────────────────

async def _run_tools(
    db: AsyncSession,
    session: AgentSession,
    calls: list[ToolCall],
    tools: list[RegisteredTool],
    *,
    dry_run: bool,
    concurrency: int,
) -> list[tuple[ToolCall, str]]:
    """Execute the model's tool calls, bounded by `concurrency`.

    A failing tool returns its error as the tool result rather than raising: the model can
    often recover by trying different arguments, and killing the whole run over one bad call
    would be worse than letting it adapt.

    The dry-run prefix is applied here rather than inside each handler, so a handler that
    forgets it cannot silently make a simulated action look real.
    """
    by_name = {t.spec.name: t for t in tools}
    limiter = asyncio.Semaphore(max(1, concurrency))

    async def execute(call: ToolCall) -> tuple[ToolCall, str]:
        tool = by_name.get(call.name)
        if tool is None:
            return call, f"Error: no tool named {call.name!r} is available to this agent."
        if "__malformed_arguments__" in call.arguments:
            return call, "Error: arguments were not valid JSON. Send them again as a JSON object."
        async with limiter:
            try:
                output = await tool.handler(call.arguments, dry_run)
            except Exception as exc:  # noqa: BLE001 — surfaced to the model, not the caller
                return call, f"Error: {type(exc).__name__}: {exc}"
        if dry_run and not output.startswith(DRY_RUN_PREFIX):
            output = f"{DRY_RUN_PREFIX} {output}"
        return call, output

    return list(await asyncio.gather(*(execute(c) for c in calls)))


# ── Session bookkeeping ────────────────────────────────────────────────────────

async def _record(
    db: AsyncSession,
    session: AgentSession,
    seq: int,
    role: MessageRole,
    content: str,
    *,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
) -> int:
    db.add(
        AgentSessionMessage(
            session_id=session.id,
            sequence=seq,
            role=role,
            content=content,
            tool_name=tool_name,
            tool_args=tool_args,
        )
    )
    db.add(session)
    await db.commit()
    return seq + 1


async def _finish(
    db: AsyncSession,
    session: AgentSession,
    status: SessionStatus,
    *,
    error: str | None = None,
) -> None:
    session.status = status
    session.error = error
    session.finished_at = datetime.now(UTC)
    db.add(session)
    await db.commit()
    await db.refresh(session)


async def _name_session(
    db: AsyncSession,
    session: AgentSession,
    client: Any,
    opening: str,
    final: str,
) -> None:
    """Give the session a short human label, the way n8n does.

    Best-effort: a naming failure must never turn a successful run into a failed one, so it
    falls back to a truncated opening message.
    """
    fallback = opening[:80]
    try:
        resp = await client.chat(
            [
                system_message(
                    "Summarise what this agent run did in 3-6 words. "
                    "Reply with the summary only, no quotes or punctuation."
                ),
                user_message(f"Request: {opening}\n\nOutcome: {final[:500]}"),
            ],
            max_tokens=32,
        )
    except Exception:  # noqa: BLE001
        session.name = fallback
    else:
        name = resp.content.strip().strip('"').splitlines()[0][:80] if resp.content else ""
        session.name = name or fallback
        # The naming call is real spend and belongs on the session's total.
        session.prompt_tokens += resp.prompt_tokens
        session.completion_tokens += resp.completion_tokens

    db.add(session)
    await db.commit()


async def _recall(db: AsyncSession, agent_id: UUID, *, exclude: UUID) -> str:
    """What this agent concluded on its last few successful runs, as a system message.

    Deliberately built from the closing message of each run rather than the whole
    transcript: the conclusion is the part worth carrying forward, and replaying full
    threads would cost more context than the current task gets.

    Best-effort — an agent that cannot recall should still run, so this never raises.
    """
    try:
        rows = await db.exec(
            select(AgentSession)
            .where(
                AgentSession.agent_id == agent_id,
                AgentSession.status == SessionStatus.succeeded,
                AgentSession.dry_run.is_(False),
                AgentSession.id != exclude,
            )
            .order_by(AgentSession.started_at.desc())
            .limit(MEMORY_RUNS)
        )
        sessions = rows.all()
    except Exception:  # noqa: BLE001 — memory is a nicety, the run is not
        return ""

    if not sessions:
        return ""

    lines: list[str] = []
    for past in reversed(sessions):
        closing = await db.exec(
            select(AgentSessionMessage.content)
            .where(
                AgentSessionMessage.session_id == past.id,
                AgentSessionMessage.role == MessageRole.assistant,
            )
            .order_by(AgentSessionMessage.sequence.desc())
            .limit(1)
        )
        summary = (closing.first() or "").strip().replace("\n", " ")
        if not summary:
            continue
        when = past.started_at.strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"- {when}: {summary[:MEMORY_CHARS]}")

    if not lines:
        return ""

    return (
        "WHAT YOU DID ON RECENT RUNS (for context only — do not repeat these actions "
        "unless the instructions call for it):\n" + "\n".join(lines)
    )


async def _tokens_used_today(db: AsyncSession, agent_id: UUID) -> int:
    """Tokens this agent has spent since midnight UTC.

    Per-agent, not per-workspace: each agent's budget is its own allowance, so a busy
    sibling agent can never starve this one out of its runs.
    """
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.exec(
        select(
            func.coalesce(
                func.sum(AgentSession.prompt_tokens + AgentSession.completion_tokens), 0
            )
        ).where(
            AgentSession.agent_id == agent_id,
            AgentSession.started_at >= midnight,
        )
    )
    return result.one()


# ── Config resolution ──────────────────────────────────────────────────────────

def _resolve_config(agent: Agent, *, use_published: bool) -> dict[str, Any]:
    """The published snapshot when there is one, otherwise the live draft."""
    if use_published and agent.published_config:
        return agent.published_config
    return {
        "instructions": agent.instructions,
        "model": agent.model,
        "model_connector_id": str(agent.model_connector_id) if agent.model_connector_id else None,
        "settings": agent.settings,
    }


def snapshot_config(agent: Agent) -> dict[str, Any]:
    """What `POST /agents/{id}/publish` freezes into `published_config`."""
    return _resolve_config(agent, use_published=False)


# ── Approval pause / resume ────────────────────────────────────────────────────

def _describe_tool_call(name: str, args: dict[str, Any]) -> str:
    """One readable sentence for the approval card."""
    a = args or {}
    if name in ("send_email",):
        return f"Send email to {a.get('to', '?')} — subject: {str(a.get('subject', ''))[:80]}"
    if name in ("reply_to_email",):
        body = str(a.get("body", ""))
        return f"Reply to email: {body[:120]}"
    if name in ("send_telegram_message",):
        msg = str(a.get("message", ""))
        return f"Send Telegram message: {msg[:120]}"
    if name in ("send_sms",):
        return f"Send SMS to {a.get('to', '?')}: {str(a.get('body', ''))[:100]}"
    if name in ("archive_email",):
        return f"Archive email {a.get('message_id', '?')}"
    return f"Call {name}({', '.join(f'{k}={str(v)[:40]}' for k, v in list(a.items())[:3])})"


async def _pause_for_approval(
    db: AsyncSession,
    session: AgentSession,
    messages: list[dict[str, Any]],
    all_calls: list[ToolCall],
    gated: ToolCall,
    seq: int,
    contexts: list[Any],
) -> None:
    """Persist the run state and suspend the session until the workspace owner decides.

    Items that the read tools surfaced this turn are immediately reserved as `in_flight`
    in the idempotency ledger.  This prevents concurrent or subsequent runs from
    re-reading the same emails while the approval is pending.  If the approval is
    rejected or the session fails, the worker releases the reservation so the next
    scheduled run can try again.
    """
    from app.integrations.registry import flush_seen

    # Reserve seen items immediately — other runs will skip them while we wait.
    await flush_seen(db, session.agent_id, contexts, session.id, status=ProcessedItemStatus.in_flight)

    # Record "waiting for approval" as a visible assistant message in the transcript.
    summary = _describe_tool_call(gated.name, gated.arguments)
    await _record(
        db, session, seq, MessageRole.assistant,
        f"[Waiting for approval] {summary}",
    )

    pending_calls = [{"id": tc.id, "name": tc.name, "args": tc.arguments} for tc in all_calls]
    # messages already has the assistant tool-call message appended by the caller.
    snapshot = list(messages)

    db.add(ApprovalRequest(
        session_id=session.id,
        agent_id=session.agent_id,
        org_id=session.org_id,
        tool_call_id=gated.id,
        tool_name=gated.name,
        tool_args=gated.arguments,
        summary=summary,
        pending_tool_calls=pending_calls,
        messages_snapshot=snapshot,
        expires_at=datetime.now(UTC) + APPROVAL_TIMEOUT,
    ))
    session.status = SessionStatus.waiting_approval
    db.add(session)
    await db.commit()


async def resume_agent(db: AsyncSession, session_id: UUID) -> AgentSession:
    """Continue a suspended session after its approval request is resolved.

    Rebuilds the message context from the snapshot stored on the ApprovalRequest, executes
    any non-gated tool calls in the same batch, injects the approval result for the gated
    call, then hands back to the normal reasoning loop.
    """
    from app.core import knowledge, notes
    from app.core.agents import calls
    from app.integrations import websearch
    from app.integrations.registry import build_tools_for_agent, confirm_seen, flush_seen

    session = await db.get(AgentSession, session_id)
    if session is None:
        raise AgentRunError(f"Session {session_id} not found.")

    req_result = await db.exec(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.session_id == session_id,
            ApprovalRequest.status.in_([ApprovalStatus.approved, ApprovalStatus.rejected, ApprovalStatus.expired]),
        )
    )
    req = req_result.first()
    if req is None:
        raise AgentRunError(f"No resolved approval request for session {session_id}.")

    agent = await db.get(Agent, session.agent_id)
    if agent is None:
        raise AgentRunError("Agent no longer exists.")

    config = _resolve_config(agent, use_published=True)
    settings = {**DEFAULT_AGENT_SETTINGS, **config.get("settings", {})}

    tools, contexts, approval_required = await build_tools_for_agent(db, agent.id)
    if settings["web_search"]:
        tools = tools + websearch.build_tools(
            live_page_access=settings["live_page_access"],
            context_size=settings["search_context"],
        )
    knowledge_tool = await knowledge.build_tool(db, agent.id)
    if knowledge_tool is not None:
        tools = tools + [knowledge_tool]
    notes_tool = await notes.build_tool(db, agent.org_id, agent.id)
    if notes_tool is not None:
        tools = tools + [notes_tool]
    # Depth-1 guard: only top-level runs get call_* tools.
    if session.triggered_by_session_id is None:
        call_tools = await calls.build_tools(db, agent, session.id)
        if call_tools:
            tools = tools + call_tools

    client = await _build_client_for(db, agent, config)

    # Restore message context from the snapshot saved at pause time.
    messages: list[dict[str, Any]] = list(req.messages_snapshot)

    # Determine the opening message for naming purposes (first user message in snapshot).
    opening = next(
        (m.get("content", "") for m in messages if m.get("role") == "user"),
        SCHEDULED_KICKOFF,
    )

    # Resume the sequence counter from where the session left off.
    seq_result = await db.exec(
        select(AgentSessionMessage.sequence)
        .where(AgentSessionMessage.session_id == session_id)
        .order_by(AgentSessionMessage.sequence.desc())
        .limit(1)
    )
    seq = (seq_result.first() or 0) + 1

    # Execute the non-gated tool calls in the batch first, then inject the approval result.
    pending = [ToolCall(id=tc["id"], name=tc["name"], arguments=tc["args"]) for tc in req.pending_tool_calls]
    free_calls = [tc for tc in pending if tc.name != req.tool_name or tc.id != req.tool_call_id]

    if free_calls:
        free_results = await _run_tools(
            db, session, free_calls, tools, dry_run=False, concurrency=settings["tool_concurrency"],
        )
        for call, output in free_results:
            seq = await _record(db, session, seq, MessageRole.tool, output, tool_name=call.name, tool_args=call.arguments)
            messages.append(tool_message(call.id, call.name, output))

    # Execute the gated tool (approved) or inject a rejection message (rejected/expired).
    gated_call = ToolCall(id=req.tool_call_id, name=req.tool_name, arguments=req.tool_args)
    if req.status == ApprovalStatus.approved:
        # Run the actual tool now that the user has signed off.
        gated_results = await _run_tools(
            db, session, [gated_call], tools, dry_run=False, concurrency=1,
        )
        _, gated_output = gated_results[0]
    else:
        reason = req.response_note or "No reason given."
        gated_output = (
            f"Rejected by the workspace owner. Reason: {reason} "
            "Stop and summarise what you could not do."
        )

    seq = await _record(db, session, seq, MessageRole.tool, gated_output, tool_name=req.tool_name, tool_args=req.tool_args)
    messages.append(tool_message(req.tool_call_id, req.tool_name, gated_output))

    session.status = SessionStatus.running
    db.add(session)
    await db.commit()

    budget = settings["daily_token_budget"]
    spent_before = await _tokens_used_today(db, agent.id)

    try:
        for iteration in range(session.iterations, settings["max_iterations"]):
            session.iterations = iteration + 1

            if budget and spent_before + session.total_tokens >= budget:
                await _finish(db, session, SessionStatus.error, error=f"Daily token budget of {budget:,} reached.")
                return session

            response = await client.chat(messages, tools=[t.spec for t in tools] or None)
            session.prompt_tokens += response.prompt_tokens
            session.completion_tokens += response.completion_tokens

            messages.append(assistant_message(response.content, response.tool_calls))
            if response.content:
                seq = await _record(db, session, seq, MessageRole.assistant, response.content)

            if not response.wants_tools:
                # Flush any new items the resumed portion surfaced, then confirm the
                # in_flight reservations from the original (pre-pause) portion.
                await flush_seen(db, agent.id, contexts, session.id)
                await confirm_seen(db, session.id)
                await _finish(db, session, SessionStatus.succeeded)
                await _name_session(db, session, client, opening, response.content)
                return session

            if approval_required:
                gated_calls = [tc for tc in response.tool_calls if tc.name in approval_required]
                if gated_calls:
                    await _pause_for_approval(db, session, messages, response.tool_calls, gated_calls[0], seq, contexts)
                    return session

            results = await _run_tools(
                db, session, response.tool_calls, tools,
                dry_run=False, concurrency=settings["tool_concurrency"],
            )
            for call, output in results:
                seq = await _record(db, session, seq, MessageRole.tool, output, tool_name=call.name, tool_args=call.arguments)
                messages.append(tool_message(call.id, call.name, output))

        await _finish(db, session, SessionStatus.error, error=f"Stopped after {settings['max_iterations']} steps.")
        return session

    except LLMError as exc:
        await _finish(db, session, SessionStatus.error, error=str(exc))
        return session
    except Exception as exc:  # noqa: BLE001
        await _finish(db, session, SessionStatus.error, error=f"{type(exc).__name__}: {exc}")
        return session


async def _build_client_for(db: AsyncSession, agent: Agent, config: dict[str, Any]) -> Any:
    connector_id = config.get("model_connector_id") or agent.model_connector_id
    if not connector_id:
        raise AgentRunError("This agent has no AI model selected.")

    connector = await db.get(Connector, UUID(str(connector_id)))
    if connector is None or connector.org_id != agent.org_id:
        raise AgentRunError("The selected AI model connector no longer exists.")
    if connector.status != ConnectorStatus.active:
        raise AgentRunError(f"The {connector.name} connector needs reconnecting.")

    api_key = decrypt_json(connector.config)["api_key"]
    try:
        return build_client(connector.type.value, api_key, config.get("model", ""))
    except LLMError as exc:
        raise AgentRunError(str(exc)) from exc
