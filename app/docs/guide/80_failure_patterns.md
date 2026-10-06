## Failure patterns

1. Publish returns "Choose an AI model" when the agent has no model connector and the plan does not include managed models.
2. Instructions were edited on the draft but not published — the live run still uses the published snapshot.
3. A connector attached with `enabled_tools: []` exposes no tools. Twilio `send_sms` goes missing this way. Omit the list to enable every tool.
4. The workspace tables connector stores `enabled_tools: []` on first attach on purpose. Tables are opted in one at a time.
5. A run in `waiting_approval` is paused on a tool that requires approval. It continues when the owner approves.
6. A run that stops on the token budget hit `daily_token_budget` in the agent's settings.
7. `search_web` for date-ordered data returns relevance order, not date order. Use a sorted listing URL or a code skill.
8. A page that is a JavaScript app returns almost no text to `fetch_page`.
9. A tool result that starts with `[simulated]` was not performed. Dry runs write nothing external.
10. `memory_set` during a dry run is kept only for that run. The next live run does not see it, so every item looks new.
11. Attaching a connector, skill, code skill, or trigger to a published agent takes effect on the next run. Publish does not undo it, and unpublishing is not required.
