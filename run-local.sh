#!/usr/bin/env bash
# Run VELA AI Assistant locally — NO docker, NO daemons. Project-local .venv only.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

# 1. Create a project-local virtualenv (prefers uv if present).
if [ ! -d .venv ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.11 .venv
  else
    "$PY" -m venv .venv
  fi
fi

# 2. Install deps into the venv.
if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -r requirements.txt
else
  ./.venv/bin/python -m pip install --upgrade pip
  ./.venv/bin/python -m pip install -r requirements.txt
fi

# 3. Ensure a .env exists (copy the template on first run).
if [ ! -f .env ]; then
  cp .env.example .env
  echo ">> Created .env from template. Fill in LLM_API_KEY, EMBED_API_KEY, DATABASE_URL before real use."
fi

# 4. Presidio downloads its spaCy model (en_core_web_lg) on first AnalyzerEngine init.
#    Nothing to do here — it happens automatically the first time /chat or /ingest runs.

# 5. Start the API (loads .env).
set -a; [ -f .env ] && . ./.env; set +a
exec ./.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
