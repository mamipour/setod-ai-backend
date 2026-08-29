#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"
source .venv/bin/activate

alembic upgrade head

# Run the API and the scheduler worker together. The worker is what actually fires
# scheduled triggers — without it agents sit ready in the DB but never execute.
# trap ensures both children are killed when this script exits (Ctrl-C or otherwise).
trap 'kill 0' EXIT

PYTHONPATH=. python -m app.tasks.worker &
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
