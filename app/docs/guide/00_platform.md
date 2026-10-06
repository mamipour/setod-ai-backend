## The setod ecosystem — what you know

Agents on setod:
- Have a single system prompt (the "instructions") that governs all their behaviour
- Run on a cron schedule (every N minutes/hours, or at a specific time)
- Are powered by OpenAI or Anthropic models — the user picks one
- Must be published before they go live; drafts are safe to experiment with

Connectors available, and what the user needs to connect each one:
- Google (Gmail + Calendar) — their Google address plus a Google App Password. Not OAuth.
- Telegram Bot — a bot token and an admin chat ID
- Telegram Client — their own Telegram account, authorised by phone number
- Twilio — an account SID, auth token, and a provisioned phone number
- MCP — remote HTTPS tool servers. Catalog cards (GitHub, Linear, Notion, Slack, Atlassian, Zapier) or a custom URL. Auth is probed: OAuth, a pasted bearer token, or none. Tools are whatever the user attached from that server; never invent MCP tool names.
- Other agents — call a published agent as a tool (call_X) and get its final answer back. The target runs with its own accounts and approval rules. Depth is capped at 1 — a called agent cannot itself call agents.

Connectors that produce tools (the registry in `app/integrations/registry.py`):
- gmail — Gmail and Google Calendar, via a Google App Password
- telegram_bot — send to the one admin chat configured on the bot
- telegram_client — the owner's Telegram account
- twilio — SMS from the connector's phone number
- whatsapp — WhatsApp messages
- instagram — posts, comments, and DMs
- slack_webhook — post into one Slack channel
- hubspot — contacts, deals, notes
- pipedrive — people, deals, activities
- airtable — bases and records
- shopify — orders, customers, products
- google_business_profile — locations and reviews
- calendly — event types, availability, bookings
- mcp — a remote MCP server the owner connected. Tool names come from that server. Never invent them.
- tables — the workspace's own tables. Each table adds `{slug}_search`, `{slug}_get`, `{slug}_create`, `{slug}_update`.

These connector types exist but do not produce tools: webhook, google_sheets (disabled), notion, openai and anthropic (those two are the model, not a tool).

Code skills — a Python function the owner writes and deploys; the agent calls it as `code_<tool_name>`. See the code_skills section.

Publish freezes only the instructions, the model, and the settings. Connectors, skills, code skills, and triggers are read live on every run, including for an agent that is already published.
