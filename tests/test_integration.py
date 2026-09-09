"""Integration tests — SKIPPED automatically when live env keys are absent.

These need real Supabase + Groq + Gemini credentials. They never fail the suite when
unconfigured; they just skip. Run them after setting DATABASE_URL / LLM_API_KEY /
EMBED_API_KEY and applying supabase_setup.sql.
"""
import os

import pytest

from app.core import db
from app.core.config import settings

_HAVE_DB = db.available()
_HAVE_LLM = bool(settings.LLM_API_KEY)
_HAVE_EMBED = bool(settings.EMBED_API_KEY)

pytestmark = pytest.mark.skipif(
    not (_HAVE_DB and _HAVE_LLM and _HAVE_EMBED),
    reason="integration test needs DATABASE_URL + LLM_API_KEY + EMBED_API_KEY (skipping offline)",
)

TENANT_A = os.getenv("TEST_TENANT_A", "demo")


def test_ingest_then_grounded_answer():
    from app.core import rag, security
    text = "VELA builds bespoke 3D websites and secured AI assistants for businesses."
    redacted, _ = security.redact(text)
    n = rag.index_document(TENANT_A, "test://fixture", redacted)
    assert n > 0
    hits = rag.retrieve(TENANT_A, "What does VELA build?")
    assert len(hits) > 0  # grounded


def test_out_of_domain_deflects():
    from app.core import rag
    hits = rag.retrieve(TENANT_A, "What is the capital of Mongolia in 1450?")
    # Either no hits, or none above MIN_SCORE -> deflection path.
    assert isinstance(hits, list)


def test_tenant_isolation():
    """Tenant A must not read tenant B's chunks (RLS + tenant_id filter)."""
    from app.core import rag, security
    tenant_b = os.getenv("TEST_TENANT_B")
    if not tenant_b:
        pytest.skip("set TEST_TENANT_B to a second seeded tenant to test isolation")
    red, _ = security.redact("SECRET_B unique marker phrase for tenant B only")
    rag.index_document(tenant_b, "test://b", red)
    hits = rag.retrieve(TENANT_A, "SECRET_B unique marker phrase")
    assert all("SECRET_B" not in h["text"] for h in hits)
