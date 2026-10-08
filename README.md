<p align="center">
  <img src="https://raw.githubusercontent.com/mamipour/setod-ai-frontend/main/public/logo.svg" alt="Setod" width="56" />
</p>

# Setod — API

Backend for [Setod](https://setod.com): an AI agent platform for small and medium size business back-office work. You write instructions in plain English, attach the accounts the agent may use, and put it on a schedule. When it wakes up, a model reads the instructions, looks at those accounts, and acts.

Actively maintained. The service people use is [setod.com](https://setod.com). This repository is the MIT source for the API, the Postgres schema, and the scheduler. The dashboard is [`setod-ai-frontend`](https://github.com/mamipour/setod-ai-frontend).

A star helps other people find the repo.

## What this is

An agent is three things: instructions, connectors, and a trigger. You bring your own OpenAI or Anthropic key. This API does not resell those tokens.

## Why this, and not a workflow graph

A tool like n8n asks you to draw every step. Setod asks for the instruction, the accounts, and when to run. The model decides the steps on that run.

Publish saves the instructions and the model settings. Connected accounts stay editable after publish, and each publish is snapshotted so you can roll back. A send, reply, or other irreversible tool can wait for approval. Every run keeps a transcript.

## What it does not do

- It does not give you a canvas of steps.
- It does not drive a browser on a desktop.

## Try it

Use the hosted app at [setod.com](https://setod.com), or run this API yourself with the steps below. Without the worker, scheduled agents sit in the database and never fire.

## Requirements

- Python 3.12 (the version this repo is deployed with; user-written code skills run on the Lambda runtime `python3.12`)
- PostgreSQL 16 or newer, with the `pgvector` extension
- A Google OAuth client used only for sign-in

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
| `CORS_ORIGINS` | Browser origins. Blank allows `localhost:3000` and `127.0.0.1:3000` in development, and must be set in production. |
| `PUBLIC_BASE_URL` | API origin for OAuth callbacks. Blank uses `http://localhost:8000` (or `APP_PORT`). No trailing slash. |

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

Apply migrations and start the API plus the worker:

```bash
./run.sh
```

The API listens on `http://localhost:8000`. OpenAPI is at `/docs` outside production.

Optional env, only if you use those features:

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — Telegram account connector
- `SLACK_MCP_CLIENT_ID` / `SLACK_MCP_CLIENT_SECRET`, and the Notion, Linear, and Atlassian pairs — so each user does not have to paste an MCP OAuth app
- `CODE_SKILLS_ENABLED` and the `AWS_USERCODE_*` variables — see below

Workspace owners add their own model keys in the dashboard. Those keys are not required in this `.env`.

## What the API runs

- **Connectors** — workspace accounts, credentials encrypted at rest. Gmail and Google Calendar (App Password), Telegram bot, Telegram account (MTProto), Twilio SMS, WhatsApp Business, Instagram, Slack, HubSpot, Pipedrive, Airtable, Shopify, Calendly, inbound webhooks, OpenAI, Anthropic, and MCP servers (GitHub, Linear, Notion, Slack, Atlassian, Zapier, or a custom HTTPS URL).
- **Schedules** — cron in the owner's timezone, stored in Postgres. Missed slots are dropped, not replayed. A run still in flight blocks the next one.
- **Copilot** — reads pages and past run traces, then suggests instructions you copy in yourself. It cannot publish.
- **Skills, notes, knowledge, approvals, agent-to-agent calls** — reusable rules, owner facts, document search (pgvector), gated writes, and calling another published agent as a tool.

## Stack

| Piece | Choice |
|---|---|
| API | FastAPI + Uvicorn |
| DB | PostgreSQL 16+ with `pgvector` |
| Scheduler | `app.tasks.worker`, claiming due rows with `FOR UPDATE SKIP LOCKED` |
| LLM | OpenAI and Anthropic, through one client |
| Auth | Google OAuth for sign-in, not for Gmail |

Schedules live in Postgres. A Redis restart would drop a cron job, and these runs are long and infrequent.

## Tests

```bash
PYTHONPATH=. python tests/e2e_slice5.py
```

`tests/e2e_slice5.py` covers owner notes and agent-to-agent calls against a live database. It makes no LLM calls. Earlier slices call a real model and spend tokens.

## Layout

```
app/
  api/            HTTP routes
  core/           agent loop, model client, schedules, knowledge, notes
  integrations/   Gmail, Calendar, Telegram, Twilio, web search, MCP
  tasks/worker.py scheduler process
  db/             SQLModel models
alembic/          migrations
```

## Code skills

User-written Python can run as one Lambda per skill, invoked through `sts:AssumeRole`, with no public function URL. Network is off unless the skill opts in.

Leave `CODE_SKILLS_ENABLED=false` until these are set: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_USERCODE_DEPLOYER_ROLE_ARN`, `AWS_USERCODE_EXEC_ROLE_ARN`, `AWS_USERCODE_EXTERNAL_ID`, `AWS_USERCODE_SUBNET_IDS`, `AWS_USERCODE_SECURITY_GROUP_ID`, and `AWS_REGION=ca-central-1`. If the flag is on in production and any of those are empty, the process refuses to start.

## License

[MIT](LICENSE)

## Related

- Dashboard: [setod-ai-frontend](https://github.com/mamipour/setod-ai-frontend)
- Product: [setod.com](https://setod.com)
