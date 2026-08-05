# =============================================================================
# Dockerfile — Sofia Voice Agent (Abans)
# =============================================================================
# Production image for the FastAPI voice agent.
# Build:  docker build -t sofia-voice-agent .
# Run:    docker run --env-file .env -p 8000:8000 sofia-voice-agent
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
# so the default PyPI torch — which bundles ~6GB of NVIDIA CUDA libraries
# (cuDNN, cuBLAS, triton, cusparse) — is pure dead weight. Installing the CPU
# build first means sentence-transformers sees torch already satisfied and never
# pulls the CUDA wheel. Cuts the image from ~9GB to ~2.5GB with no runtime change
# (MiniLM embeddings run on CPU regardless).
RUN pip install --no-cache-dir torch==2.13.0+cpu --index-url https://download.pytorch.org/whl/cpu
COPY requirements-prod.lock.txt .
RUN pip install --no-cache-dir -r requirements-prod.lock.txt

# --- Bake the embedding model into the image --------------------------------
# The KB embeds with all-MiniLM-L6-v2. Without this, the model is downloaded from
# the Hugging Face Hub on FIRST USE and cached only in the container's writable
# layer -- so every rebuild produces a container that must re-fetch it at startup.
# If the Hub is rate-limiting or down at that moment, initialize_kb() fails and
# the agent goes live with NO LISTINGS. Baking it makes startup deterministic and
# removes an external runtime dependency from a live phone line.
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('all-MiniLM-L6-v2')"

# The model is now in the image, so never phone home for it. Unauthenticated Hub
# requests are rate-limited, and a 429 on a metadata check can fail a load that
# the local cache could have served perfectly well.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# --- Application code --------------------------------------------------------
# Only the files needed at runtime are copied. We deliberately exclude:
#   .env              — secrets; injected at runtime via --env-file or orchestrator
#   test_voice*.py    — local demo scripts, not needed in production
#   venv/             — local virtualenv
#   chroma_db/        — ephemeral vector store, rebuilt on startup from knowledge_docs/
#   __pycache__/      — bytecode cache
COPY server.py knowledge_base.py supabase_client.py ./
COPY media_transport.py asterisk_ari.py asterisk_rtp.py ./
# KB backends: knowledge_base.py is the dispatcher (copied above); these are the
# two implementations it routes to, plus the SQLite-hybrid engine package.
COPY knowledge_base_chroma.py knowledge_base_sqlite.py ./
COPY kb/ ./kb/
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
