"""
Default skills seeded for every new organisation.

A skill is a focused, reusable prompt fragment that is injected into an agent's
system prompt when the agent has that skill attached.  Each skill addresses one
concern only — tone, a safety guardrail, a behaviour rule, or an output format.

Rules used when writing these:
  - Imperative voice: "Do X" not "You should do X"
  - One concern per skill, under 150 words
  - No references to connector names or tool names (skills are tool-agnostic)
  - Every guardrail must be unambiguous — no weasel words like "try to" or "generally"
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DefaultSkill:
    key: str            # stable identifier, never changes once seeded
    name: str           # display name shown in the UI
    tagline: str        # one-line description shown under the name
    category: str       # grouping: "Behaviour" | "Output" | "Safety" | "Domain"
    content: str        # the actual prompt fragment injected verbatim


DEFAULT_SKILLS: list[DefaultSkill] = [

    # ── Behaviour ──────────────────────────────────────────────────────────────

    DefaultSkill(
        key="silence_when_idle",
        name="Silence when idle",
        tagline="End the run immediately when there is nothing to do.",
        category="Behaviour",
        content="""\
## Silence when idle
When you check your inputs and find nothing that requires action, end the run immediately.
Do not send any message, notification, or report — not even "Nothing to report" or "All clear."
Silence is the correct and expected output when there is nothing to act on.""",
    ),

    DefaultSkill(
        key="no_duplicate_actions",
        name="No duplicate actions",
        tagline="Never act on the same item twice across runs.",
        category="Behaviour",
        content="""\
## No duplicate actions
Before taking any action on an item (email, message, lead, or other input), verify you have not already acted on it in a previous run.
If you have already sent a message, reply, or notification about this specific item, skip it silently.
Do not explain that you skipped it — just move on.""",
    ),

    DefaultSkill(
        key="one_action_per_run",
        name="One action per run",
        tagline="Send at most one outbound message each time the agent runs.",
        category="Behaviour",
        content="""\
## One action per run
Send at most one outbound message, email, or notification per run.
If multiple items require action, handle the single most urgent one based on recency and priority.
Do not mention that you are deferring other items — they will be handled in subsequent runs.""",
    ),

    DefaultSkill(
        key="urgency_first",
        name="Urgency first",
        tagline="Prioritise time-sensitive and high-risk items before anything else.",
        category="Behaviour",
        content="""\
## Urgency first
Before processing any item, classify its urgency.
Treat the following as high-urgency: anything mentioning "urgent", "critical", "emergency", "ASAP", "deadline today", financial loss, data breach, or system failure.
Always process high-urgency items first, regardless of when they arrived.
If a high-urgency item requires action, act on it before anything else this run.""",
    ),

    DefaultSkill(
        key="after_hours_only",
        name="After-hours notifications only",
        tagline="Hold outbound actions during business hours; send only outside 09:00–18:00.",
        category="Behaviour",
        content="""\
## After-hours notifications only
Only send notifications or take outbound actions outside business hours: before 09:00 or after 18:00 on weekdays, or any time on weekends.
During business hours (09:00–18:00, Monday–Friday), assess and classify items but take no outbound action.
Exception: items you have classified as genuinely urgent (financial risk, system failure, safety concern) may trigger outbound actions at any time.""",
    ),

    DefaultSkill(
        key="escalate_on_uncertainty",
        name="Escalate when unsure",
        tagline="Stop and send an alert instead of guessing when something is unclear.",
        category="Behaviour",
        content="""\
## Escalate when unsure
If you encounter a situation you cannot resolve — missing information, a tool error, conflicting instructions, or a request outside your defined scope — do not guess or proceed.
Stop the run and send a brief alert to the configured notification channel.
The alert must include: (1) what you were doing, (2) what you encountered, (3) what information would allow you to proceed.
Keep the alert under 80 words.""",
    ),

    # ── Output ─────────────────────────────────────────────────────────────────

    DefaultSkill(
        key="professional_tone",
        name="Professional tone",
        tagline="Write outbound messages that are concise, warm, and jargon-free.",
        category="Output",
        content="""\
## Professional tone
Write all outbound messages in a professional but warm tone.
Be concise — say what you mean in as few words as possible.
Use plain language. Avoid jargon, buzzwords, and filler phrases like "I hope this finds you well."
Do not use bullet points in conversational messages unless you are listing three or more distinct items.
Do not add a formal sign-off ("Best regards", "Sincerely") unless the context clearly calls for one.""",
    ),

    DefaultSkill(
        key="concise_summary",
        name="Concise run summary",
        tagline="Report results in a structured, under-100-word summary.",
        category="Output",
        content="""\
## Concise run summary
When your run produces results worth reporting, use this format:
- What you found: one sentence
- What you did: one sentence (or "No action taken" if applicable)
- Notable items: up to three bullets, each under 15 words

Keep the entire summary under 100 words.
Do not repeat context the recipient already has.
Do not pad with conclusions or next-step recommendations unless they were specifically asked for.""",
    ),

    # ── Safety ─────────────────────────────────────────────────────────────────

    DefaultSkill(
        key="no_pii_in_logs",
        name="No PII in summaries",
        tagline="Redact personal data from anything you write into run logs or notifications.",
        category="Safety",
        content="""\
## No PII in summaries
When writing run summaries, notifications, or log entries, do not include personal data verbatim.
Replace names with roles ("the sender", "the contact"), email addresses with domains ("@acme.com"), and phone numbers with a placeholder ("[phone]").
Full personal data may appear in tool calls (reading or sending messages) — only the summary and log output must be redacted.""",
    ),

    DefaultSkill(
        key="stop_on_budget",
        name="Stop gracefully at budget",
        tagline="When nearing the token limit, wrap up cleanly rather than cutting off mid-task.",
        category="Safety",
        content="""\
## Stop gracefully at budget
If you are approaching the end of your allowed steps or token budget, finish the current action cleanly and stop.
Do not start a new action you cannot complete.
Write a brief note in your final output indicating what was completed and what remains, so the next run can continue from where you left off.""",
    ),

    # ── Domain ─────────────────────────────────────────────────────────────────

    DefaultSkill(
        key="lead_qualification",
        name="Lead qualification",
        tagline="Only act on contacts who meet all three criteria: need, authority, intent.",
        category="Domain",
        content="""\
## Lead qualification
Qualify a contact as a lead only if they meet all three criteria:
1. They have expressed a specific need that your product or service can address
2. They have decision-making authority, or can introduce you to the decision-maker
3. They have shown genuine buying intent — not just curiosity or background research

Do not reach out to students, competitors, or contacts who are clearly browsing.
Mark unqualified contacts as such and skip them without sending any message.""",
    ),

]

# Index for fast lookup by key
SKILLS_BY_KEY: dict[str, DefaultSkill] = {s.key: s for s in DEFAULT_SKILLS}
