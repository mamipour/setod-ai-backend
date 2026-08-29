# Platform API

Backend for the agent platform: FastAPI, Postgres, and a worker that fires scheduled and inbound-triggered runs.

## Requirements

- Python 3.12+
- PostgreSQL 16+ with the `pgvector` extension
- A Google OAuth app (sign-in and the Gmail/Calendar connector)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env`. At minimum you need:

- `APP_SECRET_KEY` — any long random string
- `DATABASE_URL` — `postgresql+asyncpg://user:pass@host:5432/dbname`
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
- `ENCRYPTION_KEY` — Fernet key used to encrypt stored connector credentials

Generate the encryption key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Apply migrations, then start the API and the worker together:

```bash
./run.sh
```

The API listens on `http://localhost:8000`. Docs are at `/docs` outside production.

`PUBLIC_BASE_URL` is the origin used for OAuth callbacks and inbound webhooks. Leave it blank in local development (`http://localhost:8000`). In production set it to the public API origin, with no trailing slash.

## Tests

```bash
PYTHONPATH=. python tests/e2e_slice5.py
```

`e2e_slice5.py` covers owner notes and agent-to-agent calls against a live database and makes no LLM calls. Slices 1–4 drive a real model and spend tokens.
