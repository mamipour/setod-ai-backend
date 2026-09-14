<p align="center">
  <img src="https://raw.githubusercontent.com/mamipour/setod-ai-frontend/main/public/logo.svg" alt="Setod" width="56" />
</p>

<h1 align="center">Setod — API</h1>

Backend for [Setod](https://setod.com): an AI agent platform for small-business back-office work. You write instructions in plain English, attach the accounts the agent may use, and put it on a schedule. When it wakes up, a model you already pay for (OpenAI or Anthropic) reads the instructions, looks at those accounts, and acts.

This repo is the FastAPI API, Postgres schema, and the scheduler worker. The dashboard lives in [`setod-ai-frontend`](https://github.com/mamipour/setod-ai-frontend).

## What it does

An agent is three things: **instructions**, **connectors**, and a **trigger**. Everything else exists to make those safe to run unattended.

- **Connectors** — workspace-scoped accounts, credentials encrypted at rest. Google (Gmail + Calendar via App Password), Telegram bot, Telegram account (MTProto), Twilio SMS, OpenAI, Anthropic, and MCP servers (GitHub, Linear, Notion, Slack, Atlassian, Zapier, or a custom HTTPS URL).
- **Tools** — come from the connector you attach. Reading tools skip items the agent already handled; irreversible writes (reply, archive, send) are recorded the moment they happen.
- **Schedules** — cron in the owner's timezone, stored in Postgres. Missed slots are dropped, not replayed. A run still in flight blocks the next one.
- **Draft / publish** — editing never changes what is live until you publish. Each publish is snapshotted so you can roll back.
- **Copilot** — a read-only prompt engineer that can search the web, fetch pages, and read past run traces, then suggest instructions you copy in yourself.
- **Skills, notes, knowledge, approvals, agent-to-agent calls** — reusable behaviour rules, owner facts/tasks, document RAG (pgvector), gated write tools, and calling another published agent as a tool.

You bring your own model keys. Setod does not resell tokens.

## Stack

| Piece | Choice |
|---|---|
| API | FastAPI + Uvicorn |
| DB | PostgreSQL 16+ with `pgvector` |
| Scheduler | A polling worker (`app.tasks.worker`). Claims due rows with `FOR UPDATE SKIP LOCKED`. |
| LLM | OpenAI and Anthropic, through one client |
| Auth | Google OAuth for sign-in (not for Gmail) |

Schedules live in Postgres, not Redis. Agent runs are long and infrequent; losing a cron job on a Redis restart is worse than extra queue throughput.

## Requirements

- Python 3.12+
- PostgreSQL 16+ with the `pgvector` extension
- A Google OAuth client used **only for sign-in** (Gmail/Calendar use a Google App Password)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Minimum `.env`:

| Variable | Purpose |
|---|---|
| `APP_SECRET_KEY` | Any long random string |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/dbname` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Sign-in only |
| `ENCRYPTION_KEY` | Fernet key for connector credentials |
| `CORS_ORIGINS` | Browser origins. Blank uses `localhost:3000` and `127.0.0.1:3000` in development. Must be set in production. |
| `PUBLIC_BASE_URL` | API origin for OAuth callbacks. Blank → `http://localhost:8000`. Production: no trailing slash. |

Generate the encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Create the database and enable the extension:

```sql
CREATE DATABASE setod;
\c setod
CREATE EXTENSION IF NOT EXISTS vector;
```

Apply migrations and start the API plus worker:

```bash
./run.sh
```

The API listens on `http://localhost:8000`. OpenAPI is at `/docs` outside production.

`./run.sh` starts both processes. Without the worker, agents sit ready in the database and never fire.

Optional env (only if you use those features):

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — Telegram account (user) connector
- `SLACK_MCP_CLIENT_ID` / `…_SECRET` (and the Notion / Linear / Atlassian pairs) — skip asking each user to paste an MCP OAuth app
- `TWILIO_*` — unused for per-workspace Twilio connectors; those credentials live on the connector

Workspace owners add their own OpenAI/Anthropic keys and an optional Tavily key for web search in the dashboard. They are not server env vars.

## Tests

```bash
PYTHONPATH=. python tests/e2e_slice5.py
```

`e2e_slice5.py` covers owner notes and agent-to-agent calls against a live database and makes no LLM calls. Earlier slices drive a real model and spend tokens.

## Layout

```
app/
  api/            HTTP routes (agents, auth, connectors, approvals, skills, notes, webhooks)
  core/           ReAct loop, LLM client, schedules, knowledge, notes
  integrations/   Gmail, Calendar, Telegram, Twilio, web search, MCP
  tasks/worker.py scheduler process
  db/             SQLModel models
alembic/          migrations
```

## License

[MIT](LICENSE)

## Related

- Dashboard: [setod-ai-frontend](https://github.com/mamipour/setod-ai-frontend)
- Product: [setod.com](https://setod.com)
