# =============================================================================
# Dockerfile — Kavya Voice Agent (Mosvold Boutique Hotels)
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
# Install CPU-only torch FIRST from the PyTorch CPU index. This box has no GPU,
# so the default PyPI torch — which bundles ~6GB of NVIDIA CUDA libraries — is
# pure dead weight. Installing the CPU build first means sentence-transformers
# never pulls the CUDA wheel. Cuts the image from ~9GB to ~2.5GB, no runtime change.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements-prod.lock.txt .
RUN pip install --no-cache-dir -r requirements-prod.lock.txt

# --- Application code --------------------------------------------------------
# Only the files needed at runtime are copied. We deliberately exclude:
#   .env              — secrets; injected at runtime via --env-file or orchestrator
#   test_voice*.py    — local demo scripts, not needed in production
#   venv/             — local virtualenv
#   chroma_db/        — ephemeral vector store, rebuilt on startup from knowledge_docs/
#   __pycache__/      — bytecode cache
# WARNING: this is an explicit ALLOWLIST, not a glob. A new module that
# server.py or tools.py imports MUST be added here or the image will not
# contain it. Fast-mode deploys hide the mistake — they `docker cp` the .py
# straight into the running container, so prod keeps working while the image
# stays broken — and the next BUILD-mode deploy then crashes on import.
# That is exactly how handover.py took prod down on 2026-07-31
# (ModuleNotFoundError: No module named 'handover'), having been added in the
# 2026-07-30 handover-failsafe work and only ever hot-swapped in.
# If you add a module, add it here in the SAME commit.
COPY server.py media_stream_server.py tools.py booking_api.py post_call.py knowledge_base.py yanolja_client.py yanolja_service.py dashboard_client.py handover.py ./
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
