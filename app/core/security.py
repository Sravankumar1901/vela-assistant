"""Security layer: PII redaction (Presidio), prompt-injection defense, audit log, auth."""
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Tuple

from .config import settings

# ---- PII redaction (Presidio, with regex fallback) -------------------------
_ANALYZER = None
_ANONYMIZER = None
try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    _ANALYZER = AnalyzerEngine()
    _ANONYMIZER = AnonymizerEngine()
except Exception:  # Presidio not available -> regex fallback keeps the app running
    _ANALYZER = None

# Order matters: match specific structured patterns (SSN, card) BEFORE the broad phone
# pattern, which would otherwise swallow SSN-/card-shaped digit runs.
_FALLBACK = [
    ("EMAIL", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("PHONE", re.compile(r"(?:(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?){2,4}\d{2,4})")),
]


def redact(text: str, mode: str = "strict") -> Tuple[str, list]:
    """Return (redacted_text, findings). Applied BEFORE embedding or LLM.

    Two modes, because a VISITOR's message and a BUSINESS's own content need different rules:

      mode="strict" (default — for visitor/user input):
        Presidio broad NER (names, locations, dates, orgs, …) PLUS the structured regex net
        (email/phone/card/SSN). Protects the person typing — strip everything identifying.

      mode="business" (for a business's OWN ingested knowledge — website, About, FAQ, pricing):
        ONLY the structured regex net (email/phone/card/SSN). KEEPS the brand name, people,
        places and dates the business wants the bot to actually use, while still stripping any
        customer email/phone/card/SSN that happens to appear in the content.

    Either way, raw structured PII never leaves this function.
    """
    if not settings.PII_REDACTION or not text:
        return text, []
    findings: set = set()
    red = text
    # Presidio's broad NER runs only in strict mode (it is what over-redacts a brand/city).
    if mode != "business" and _ANALYZER is not None:
        try:
            results = _ANALYZER.analyze(text=text, language="en")
            if results:
                findings.update(r.entity_type for r in results)
                red = _ANONYMIZER.anonymize(text=text, analyzer_results=results).text
        except Exception:
            pass  # fall through to regex only
    # Regex safety net — ALWAYS runs (both modes): guarantees structured-PII coverage.
    for label, pat in _FALLBACK:
        if pat.search(red):
            findings.add(label)
            red = pat.sub(f"<{label}>", red)
    return red, sorted(findings)


# ---- Prompt-injection defense ---------------------------------------------
_INJECTION_PAT = re.compile(
    r"(ignore (all|previous|above).{0,30}instructions|disregard.{0,20}(instructions|prompt)|"
    r"you are now|system prompt|reveal.{0,20}(prompt|instructions)|act as|jailbreak|"
    r"pretend to be|override.{0,20}rules)",
    re.IGNORECASE,
)


def injection_flag(text: str) -> bool:
    return bool(text and _INJECTION_PAT.search(text))


def sanitize_user_input(text: str, max_len: int = 2000) -> str:
    text = (text or "").strip()[:max_len]
    return text.replace("\x00", "")


# ---- Auth ------------------------------------------------------------------
def hash_key(api_key: str) -> str:
    """sha256 hex of an API key. Keys are stored/compared as hashes, never plaintext."""
    import hashlib
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()


def resolve_tenant(api_key: str) -> str | None:
    """Return tenant id for a valid API key, else None.

    DB-backed: hashes the presented key and looks it up in the `tenants` table.
    Falls back to the in-memory bootstrap seed (hashed compare) when the DB is
    unavailable, so local dev without Supabase still authenticates.
    """
    if not api_key:
        return None
    key_hash = hash_key(api_key)

    # 1) Primary: DB lookup by hash.
    try:
        from . import db
        if db.available():
            tenant = db.resolve_tenant_by_hash(key_hash)
            if tenant:
                return tenant
            # DB is up but no match -> deny (a removed tenant is revoked).
            return None
    except Exception:
        pass  # DB error -> fall through to seed for resilience

    # 2) Fallback: bootstrap seed (hashed, constant-time compare).
    import hmac
    for tenant, key in settings.tenant_map().items():
        if hmac.compare_digest(key_hash, hash_key(key)):
            return tenant
    return None


# ---- Rate limiting (in-memory, per tenant) ---------------------------------
_BUCKET: dict = {}


def rate_limited(tenant: str) -> bool:
    now = int(time.time() // 60)
    k = (tenant, now)
    _BUCKET[k] = _BUCKET.get(k, 0) + 1
    # prune old
    for old in [kk for kk in _BUCKET if kk[1] < now]:
        _BUCKET.pop(old, None)
    return _BUCKET[k] > settings.RATE_LIMIT_PER_MIN


# ---- Audit log (structured JSONL + optional Langfuse) ----------------------
_LF = None
if settings.LANGFUSE_ENABLED:
    try:
        from langfuse import Langfuse
        _LF = Langfuse(public_key=settings.LANGFUSE_PUBLIC_KEY,
                       secret_key=settings.LANGFUSE_SECRET_KEY, host=settings.LANGFUSE_HOST)
    except Exception:
        _LF = None


def audit(event: dict):
    """Append an immutable audit record. NEVER logs raw PII — logs redacted text + findings only."""
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    try:
        os.makedirs(os.path.dirname(settings.AUDIT_LOG_PATH), exist_ok=True)
        with open(settings.AUDIT_LOG_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass
    if _LF is not None:
        try:
            _LF.trace(name=event.get("event", "chat"), metadata=event)
        except Exception:
            pass
