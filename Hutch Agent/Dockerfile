# =============================================================================
# Dockerfile — Hutch Voice Agent (Selina)
# =============================================================================
# Production image for the FastAPI voice agent.
# Build:  docker build -t hutch-voice-agent .
# Run:    docker run --env-file .env -p 8000:8000 hutch-voice-agent
# =============================================================================

# --- Base image --------------------------------------------------------------
FROM python:3.11-slim

# --- Environment variables ---------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# --- System dependencies ----------------------------------------------------
# curl is required for the HEALTHCHECK probe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# --- Working directory -------------------------------------------------------
WORKDIR /app

# --- Python dependencies (cached layer) -------------------------------------
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

# --- Application code --------------------------------------------------------
# Only the files needed at runtime are copied. Deliberately excluded:
#   .env              — secrets; injected at runtime via --env-file or orchestrator
#   venv/             — local virtualenv
#   chroma_db*/       — ephemeral vector store, rebuilt on startup from knowledge_docs/
#   __pycache__/      — bytecode cache
COPY server.py knowledge_base.py handover.py tools.py ./
COPY media_transport.py smartpbx_protocol.py smartpbx_gateway.py smartpbx_transport.py smartpbx_session.py ./
COPY knowledge_docs/ ./knowledge_docs/

# Fail the build fast if the module closure does not import cleanly, rather
# than discovering a missing module only when the container starts serving
# real Dialog traffic.
RUN python -c "import server"

# --- Port --------------------------------------------------------------------
EXPOSE 8000

# --- Health check ------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# --- Run command -------------------------------------------------------------
# Single worker is intentional: sentence-transformers loads the embedding model
# (~400 MB-1 GB RAM) per worker. With async FastAPI + WebSockets, one worker
# handles many concurrent calls just fine.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
