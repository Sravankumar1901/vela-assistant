# Cloudflare Containers image for the VELA RAG backend (regex-only PII build).
# Cloudflare's runtime is linux/amd64 — pin it so an arm64 Mac still produces a
# runnable image (wrangler builds this locally via Docker, then pushes to CF).
FROM --platform=linux/amd64 python:3.11-slim

# Faster, quieter Python; no .pyc, unbuffered logs for CF log streaming.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /srv

# Install deps first for layer caching. psycopg[binary] ships its own libpq, so
# no apt build toolchain is needed for the slim build.
COPY requirements-container.txt .
RUN pip install --no-cache-dir -r requirements-container.txt

# App code only (the .dockerignore keeps .venv/.env/data/tests out of the image).
COPY app ./app

EXPOSE 8080
# Single worker: the container scales horizontally via more instances, not threads.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
