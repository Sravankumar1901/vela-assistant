"""RAG store on Supabase Postgres + pgvector (replaces Qdrant).

One table:  chunks(id, tenant_id, content, embedding vector(768), source, created_at)
  - Retrieval = cosine similarity via the `<=>` cosine-distance operator.
  - similarity = 1 - cosine_distance ; we keep only hits with similarity >= MIN_SCORE.
  - Every query runs inside a tenant-scoped RLS session AND filters by tenant_id (defense in depth),
    so a tenant can never read another tenant's vectors.

Chunking/overlap is reused from the original implementation.
"""
from __future__ import annotations

from . import db, llm
from .config import settings


def chunk_text(text: str) -> list[str]:
    text = " ".join((text or "").split())
    size, overlap = settings.CHUNK_CHARS, settings.CHUNK_OVERLAP
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += max(1, size - overlap)
    return [c for c in out if c.strip()]


def index_document(tenant: str, source: str, text: str) -> int:
    """text must already be PII-redacted. Returns number of chunks stored."""
    chunks = chunk_text(text)
    if not chunks:
        return 0
    vectors = llm.embed(chunks)  # -> list[list[float]], EMBED_DIM long each
    with db.tenant_session(tenant) as conn:
        with conn.cursor() as cur:
            for content, vec in zip(chunks, vectors):
                cur.execute(
                    """
                    INSERT INTO chunks (tenant_id, content, embedding, source)
                    VALUES (%s, %s, %s::vector, %s)
                    """,
                    (tenant, content, _to_vector_literal(vec), source),
                )
        conn.commit()
    return len(chunks)


def retrieve(tenant: str, query: str) -> list[dict]:
    """Return [{score, text, source}] with cosine similarity >= MIN_SCORE. query already redacted."""
    qvec = llm.embed([query])[0]
    with db.tenant_session(tenant, readonly=False) as conn:
        with conn.cursor() as cur:
            # `embedding <=> q` is cosine DISTANCE in [0,2]; similarity = 1 - distance.
            # Order by distance asc (closest first), then threshold on similarity.
            cur.execute(
                """
                SELECT content,
                       source,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM chunks
                WHERE tenant_id = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (_to_vector_literal(qvec), tenant, _to_vector_literal(qvec), settings.TOP_K),
            )
            rows = cur.fetchall()
    out = []
    for content, source, similarity in rows:
        if similarity is None or float(similarity) < settings.MIN_SCORE:
            continue
        out.append({"score": float(similarity), "text": content or "", "source": source or ""})
    return out


def _to_vector_literal(vec) -> str:
    """pgvector accepts a bracketed string literal e.g. '[0.1,0.2,...]'.

    Using the string form keeps this working whether or not the pgvector python adapter
    is registered on the connection, and casts cleanly to `vector` in SQL.
    """
    return "[" + ",".join(f"{float(x):.8f}" for x in vec) + "]"
