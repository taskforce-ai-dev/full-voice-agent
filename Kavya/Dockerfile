# =============================================================================
# Dockerfile — Kavya Voice Agent (Treehouse Chalets)
# =============================================================================
# Production image for the FastAPI voice agent.
# Build:  docker build -t kavya-voice-agent .
# Run:    docker run --env-file .env -p 8000:8000 kavya-voice-agent
# =============================================================================

# --- Base image --------------------------------------------------------------
# python:3.11-slim keeps the image small (~150 MB) while including the C
# toolchain headers needed to compile sentence-transformers / torch wheels.
FROM python:3.11-slim

# --- Environment variables ---------------------------------------------------
# PYTHONUNBUFFERED=1  — force stdout/stderr to be unbuffered so logs appear
#                       immediately in `docker logs`.
# PYTHONDONTWRITEBYTECODE=1 — prevent .pyc files; saves a tiny bit of disk
#                              and avoids stale bytecode issues.
# PORT=8000           — default listening port (matches EXPOSE below).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# --- System dependencies ----------------------------------------------------
# curl is required for the HEALTHCHECK probe.
# --no-install-recommends keeps the layer lean; rm the apt cache afterwards.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# --- Working directory -------------------------------------------------------
WORKDIR /app

# --- Python dependencies (cached layer) -------------------------------------
# Copy requirements-prod.txt first so that `pip install` is cached as long as
# dependencies don't change — even if application code does.
# requirements-prod.txt intentionally excludes pyaudio (needs PortAudio C lib,
# only used in local demo) and azure-cognitiveservices-speech (backup TTS).
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

# --- Application code --------------------------------------------------------
# Only the files needed at runtime are copied. We deliberately exclude:
#   .env              — secrets; injected at runtime via --env-file or orchestrator
#   test_voice*.py    — local demo scripts, not needed in production
#   venv/             — local virtualenv
#   chroma_db/        — ephemeral vector store, rebuilt on startup from knowledge_docs/
#   __pycache__/      — bytecode cache
COPY server.py media_stream_server.py tools.py booking_api.py post_call.py knowledge_base.py yanolja_client.py yanolja_service.py dashboard_client.py ./
COPY knowledge_docs/ ./knowledge_docs/

# --- Port --------------------------------------------------------------------
EXPOSE 8000

# --- Health check ------------------------------------------------------------
# Hits the /health endpoint every 30 s. Twilio and load balancers can also use
# this endpoint for their own liveness probes.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# --- Run command -------------------------------------------------------------
# Single worker is intentional: sentence-transformers loads the embedding model
# (~400 MB–1 GB RAM) per worker. With async FastAPI + WebSockets, one worker
# handles many concurrent calls just fine. Scale horizontally (more containers)
# instead of vertically (more workers) if needed.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
