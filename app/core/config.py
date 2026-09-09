"""Central config — all via env, no hardcoded secrets.

TARGET STACK (fully hosted, zero-docker):
  - Chat LLM     : Groq (OpenAI-compatible)          -> LLM_*
  - Embeddings   : Google Gemini text-embedding-004  -> EMBED_*  (768-dim)
  - Vector store : Supabase Postgres + pgvector      -> DATABASE_URL
  - Relational   : Supabase Postgres (same DB)        -> DATABASE_URL
  - Read-only SQL: separate read-only role            -> READONLY_DATABASE_URL
Everything stays swappable via env with no code change.
"""
import os


def _bool(v: str, default: bool = False) -> bool:
    return str(os.getenv(v, str(default))).lower() in ("1", "true", "yes", "on")


class Settings:
    # --- Chat LLM (OpenAI-compatible; defaults to Groq free tier) ---
    # Swap to OpenAI by setting LLM_BASE_URL=https://api.openai.com/v1 + LLM_MODEL=gpt-4o-mini.
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")            # Groq / OpenAI key
    LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.1-8b-instant")

    # --- Embeddings (Google Gemini, OpenAI-compatible endpoint) ---
    # EMBED_PROVIDER: "openai" (use the OpenAI-compatible client, default) or
    #                 "gemini" (use the google-genai SDK) — same embed() interface either way.
    EMBED_PROVIDER = os.getenv("EMBED_PROVIDER", "openai").lower()
    EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    EMBED_API_KEY = os.getenv("EMBED_API_KEY", "")        # Gemini (Google AI Studio) key
    EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-004")
    EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))

    # --- Database (Supabase Postgres; holds pgvector chunks + relational data) ---
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    # Read-only role for Chat-to-SQL (SELECT-only grants). NO fallback to the write role:
    # if this is unset, Chat-to-SQL is DISABLED rather than run on a privileged role
    # (audit finding H3). Point it at a role with SELECT-only grants on the allowed tables.
    READONLY_DATABASE_URL = os.getenv("READONLY_DATABASE_URL", "")

    # --- Security ---
    # Bootstrap seed: "tenant1:key1,tenant2:key2". On startup these are UPSERTED into the
    # tenants table with the key stored as a sha256 hash (never plaintext). Auth then reads the DB.
    # NO default — a deploy without TENANT_KEYS (and no DB tenants) authenticates NOBODY, rather
    # than shipping a publicly-known working key (audit finding H2). Use high-entropy random keys.
    TENANT_KEYS = os.getenv("TENANT_KEYS", "")
    PII_REDACTION = _bool("PII_REDACTION", True)
    RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "30"))
    # Admin portal auth: a high-entropy token gating /admin (X-Admin-Token). No default —
    # if unset the admin portal is disabled (fails closed), same posture as TENANT_KEYS.
    ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
    # CORS allow-list. "*" (default) for local dev; in production set to the exact
    # site origin(s), comma-separated, e.g. "https://byvela.online" so the public
    # widget token can only be exercised from the real site (origin-lock).
    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

    # --- Infrastructure / Cloudflare (optional) ---
    # When BOTH are set, the admin Infrastructure page shows live Cloudflare Pages/Workers
    # counts. Left blank the page still renders (it just reports "not connected").
    CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
    CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

    # --- Retrieval / anti-hallucination ---
    TOP_K = int(os.getenv("TOP_K", "5"))
    # Cosine SIMILARITY threshold in [0,1]; below this => "I don't know" deflection.
    MIN_SCORE = float(os.getenv("MIN_SCORE", "0.35"))
    CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1200"))
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "150"))

    # --- Chat-to-SQL guardrails ---
    SQL_ROW_CAP = int(os.getenv("SQL_ROW_CAP", "100"))
    SQL_TIMEOUT_MS = int(os.getenv("SQL_TIMEOUT_MS", "3000"))

    # --- Observability ---
    LANGFUSE_ENABLED = _bool("LANGFUSE_ENABLED", False)
    LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
    AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "data/audit.log.jsonl")

    # --- DEPRECATED (old local stack, kept only so nothing imports-errors) ---
    # QDRANT_URL / QDRANT_API_KEY removed — vector store is now pgvector.

    def allowed_origins_list(self) -> list:
        """Parse ALLOWED_ORIGINS into a list for the CORS middleware."""
        v = (self.ALLOWED_ORIGINS or "").strip()
        if v in ("", "*"):
            return ["*"]
        return [o.strip() for o in v.split(",") if o.strip()]

    def tenant_map(self) -> dict:
        """Parse the bootstrap seed env into {tenant_id: plaintext_key}."""
        out = {}
        for pair in self.TENANT_KEYS.split(","):
            pair = pair.strip()
            if ":" in pair:
                t, k = pair.split(":", 1)
                out[t.strip()] = k.strip()
        return out


settings = Settings()
