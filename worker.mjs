// Cloudflare Worker that fronts the VELA RAG backend running in a Container.
// The Worker is the public edge; it forwards requests to the FastAPI container
// on port 8080 and injects the backend's runtime config/secrets as env vars.
import { Container, getContainer } from "@cloudflare/containers";

export class VelaBackend extends Container {
  defaultPort = 8080; // uvicorn listens here (see Dockerfile)
  sleepAfter = "10m"; // scale to zero after 10m idle → you only pay while active

  // Runtime config passed into the container process. Non-secret values come from
  // `vars` in wrangler.jsonc; secrets come from `wrangler secret put` — both land
  // on `this.env`. Defaults in app/core/config.py cover everything not set here.
  envVars = {
    // Chat LLM (Gemini via OpenAI-compatible endpoint)
    LLM_BASE_URL: this.env.LLM_BASE_URL,
    LLM_API_KEY: this.env.LLM_API_KEY,
    LLM_MODEL: this.env.LLM_MODEL,
    // Embeddings (Gemini SDK, 768-dim)
    EMBED_PROVIDER: this.env.EMBED_PROVIDER,
    EMBED_API_KEY: this.env.EMBED_API_KEY,
    EMBED_MODEL: this.env.EMBED_MODEL,
    // Supabase Postgres + pgvector: write role + SELECT-only role for Chat-to-SQL
    DATABASE_URL: this.env.DATABASE_URL,
    READONLY_DATABASE_URL: this.env.READONLY_DATABASE_URL,
    // Auth / security
    TENANT_KEYS: this.env.TENANT_KEYS,
    ADMIN_TOKEN: this.env.ADMIN_TOKEN,
    ALLOWED_ORIGINS: this.env.ALLOWED_ORIGINS,
  };
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;

    // Defense in depth: the token-gated admin API isn't needed at the public edge
    // yet (the admin portal isn't deployed). Keep it off the internet for now.
    if (path === "/admin" || path.startsWith("/admin/")) {
      return new Response("Not found", { status: 404 });
    }

    // Everything else → the single backend container instance. All app endpoints
    // are auth-gated (widget token / tenant API key), hardened per the audit.
    return getContainer(env.VELA_BACKEND).fetch(request);
  },
};
