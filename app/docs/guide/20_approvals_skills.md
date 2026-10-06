Human approval:
- Individual tools can be flagged "requires approval" — the agent will pause and wait before executing them
- Useful for any action that is irreversible or external-facing

Skills:
- Skills are reusable prompt fragments stored in the org's Skills library
- Each skill covers one concern: a behaviour rule, output format, or safety guardrail
- Skills are attached per-agent from the Agent tab → Skills section
- When attached, a skill's content is injected into the agent's system prompt automatically — the user does not need to copy its text into the instructions
- Default skills available in every org: Silence when idle, No duplicate actions, One action per run, Urgency first, After-hours notifications only, Escalate when unsure, Professional tone, Concise run summary, No PII in summaries, Stop gracefully at budget, Lead qualification
- "No duplicate actions" covers in-run safety (prevents the agent calling the same write tool twice within a single run). It does NOT handle cross-run deduplication of fetched web/CSV content — for that, the user must enable "Remember past runs" in the agent's Settings tab.
- Users can edit any skill or create their own from the Skills page (sidebar → Skills)
- When writing a prompt, you should NOT duplicate behaviour that a skill already handles — instead tell the user to attach the relevant skill
