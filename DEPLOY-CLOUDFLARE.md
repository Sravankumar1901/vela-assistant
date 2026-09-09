# Deploy the VELA RAG backend to Cloudflare Containers

All-Cloudflare hosting for the FastAPI RAG backend. The site (byvela.online) already
runs on Cloudflare Pages; this puts the backend on **Cloudflare Containers** (a Worker
that fronts a Docker container) at **`api.byvela.online`**, so the "Ask VELA" chat works.

```
byvela.online       → Cloudflare Pages   (site, already live)
api.byvela.online   → Worker → Container  (this backend — regex-only PII)
      ├─ Supabase Postgres + pgvector   (DB, external)
      └─ Google Gemini                  (chat + embeddings, external)
```

## Files this adds (all in `vela-assistant/`)
- `Dockerfile` + `.dockerignore` — the container image (regex-only, `python:3.11-slim`)
- `requirements-container.txt` — slim deps (no Presidio/spaCy/numpy)
- `worker.mjs` — the edge Worker (forwards to the container, blocks `/admin/*` publicly)
- `wrangler.jsonc` — Worker + container config (`basic` = 1 GiB instance)
- `package.json` — `@cloudflare/containers` + `wrangler`
- `scripts/set-cf-secrets.sh` — pushes the 6 secrets from your local `.env`

---

## One-time prerequisites (YOU)
1. **Workers Paid plan ($5/mo)** — Containers require it. Enable at
   dash.cloudflare.com → Workers & Pages → Plans. (Idle containers are free on top of it.)
2. **Docker running** — already installed. `docker info` should succeed.

## Deploy steps
Run these from `vela-assistant/`:

```bash
# 1. Install the edge deps (Worker + wrangler)
npm install

# 2. Log in to Cloudflare (opens a browser — YOU authorize)
npx wrangler login

# 3. First deploy — builds the image locally, pushes to Cloudflare's registry,
#    creates the Worker + container. Takes a few minutes the first time.
npx wrangler deploy

# 4. Push the 6 secrets from your local .env into the Worker
bash scripts/set-cf-secrets.sh

# 5. Redeploy so the container picks up the secrets, then smoke-test
npx wrangler deploy
curl -s https://vela-assistant-api.<your-subdomain>.workers.dev/health
```

`/health` returning ok = the container is live and talking to Supabase + Gemini.

## Put it on api.byvela.online
Once `/health` works on the `workers.dev` URL, add the custom domain:
- **Dashboard:** Workers & Pages → `vela-assistant-api` → Settings → Domains & Routes →
  Add → Custom domain → `api.byvela.online`. Cloudflare adds the DNS record automatically.
- **or** add to `wrangler.jsonc` and redeploy:
  ```jsonc
  "routes": [{ "pattern": "api.byvela.online", "custom_domain": true }]
  ```

## Wire the site to the backend (ME, after the API is live)
1. In the **Pages** project env vars (Production): set
   `NEXT_PUBLIC_ASSISTANT_API = https://api.byvela.online` and
   `NEXT_PUBLIC_ASSISTANT_WIDGET_TOKEN = wt_…`.
2. Add `https://api.byvela.online` to the CSP **`connect-src`** in the site's `_headers`.
3. Redeploy the site → the "Ask VELA" launcher renders and **chat works** (Vera already does).

---

## Before-public security checklist (going live = do these)
- [x] CORS origin-locked to `byvela.online` (via `ALLOWED_ORIGINS`)
- [x] `/admin/*` blocked at the edge Worker
- [x] Read-only role wired (`READONLY_DATABASE_URL`) for Chat-to-SQL
- [ ] Confirm `TENANT_KEYS` / `ADMIN_TOKEN` are strong (they are, from `.env`)
- [ ] (Optional, M-level) SQL-result PII in `conversations.answer`; transaction-pooler GUC — follow-ups

## Notes / knobs
- **Cost:** ~$5/mo (Workers Paid). Container scales to zero after 10m idle (`sleepAfter`),
  so you only pay while a request is being handled. First chat after idle has a small
  cold-start (~a few seconds).
- **Full Presidio later:** add `presidio-analyzer`/`presidio-anonymizer`/`spacy`/`numpy`
  back to a requirements file, download the model in the Dockerfile, and bump
  `instance_type` to `"standard-1"` (4 GiB) in `wrangler.jsonc`.
- **Logs:** `npx wrangler tail` streams the Worker; container stdout shows in the dashboard.
