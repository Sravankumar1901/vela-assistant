#!/usr/bin/env bash
# Push the backend's secrets from the local .env into the Cloudflare Worker.
# Run from anywhere AFTER `npx wrangler login` (and after the first `wrangler deploy`
# has created the "vela-assistant-api" Worker). Values are read from .env at runtime
# and piped via stdin — they never land in shell history or process args.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env found at project root ($(pwd))." >&2; exit 1; }
set -a; . ./.env; set +a

put() {
  local name="$1"; local val="${2:-}"
  if [ -z "$val" ]; then echo "!! $name empty in .env — skipping"; return; fi
  printf '%s' "$val" | npx wrangler secret put "$name"
  echo ">> set $name"
}

put LLM_API_KEY           "${LLM_API_KEY:-}"
put EMBED_API_KEY         "${EMBED_API_KEY:-}"
put DATABASE_URL          "${DATABASE_URL:-}"
put READONLY_DATABASE_URL "${READONLY_DATABASE_URL:-}"
put TENANT_KEYS           "${TENANT_KEYS:-}"
put ADMIN_TOKEN           "${ADMIN_TOKEN:-}"

echo "Done. Run 'npx wrangler deploy' to roll the container onto the new secrets."
