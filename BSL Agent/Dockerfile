# =============================================================================
# Dockerfile — BSL Voice Agent (Bank of Sri Lanka)
# =============================================================================
# Production image for the FastAPI voice agent.
# Build:  docker build -t bsl-voice-agent .
# Run:    docker run --env-file .env -p 8000:8000 bsl-voice-agent
# =============================================================================

# --- Base image --------------------------------------------------------------
FROM python:3.11-slim

# --- Environment variables ---------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# --- System dependencies ----------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# --- Working directory -------------------------------------------------------
WORKDIR /app

# --- Python dependencies (cached layer) -------------------------------------
# Install CPU-only torch FIRST from the PyTorch CPU index. This box has no GPU,
# so the default PyPI torch — which bundles ~6GB of NVIDIA CUDA libraries — is
# pure dead weight. Installing the CPU build first means sentence-transformers
# never pulls the CUDA wheel. Cuts the image from ~9GB to ~2.5GB, no runtime change.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

# --- Application code --------------------------------------------------------
# bsl_api.py replaces SLIC's claim_api.py. account_state.py replaces SLIC's
# active_session.py (no cross-call continuation needed).
COPY server.py tools.py bsl_api.py mock_db.py account_state.py post_call.py knowledge_base.py mock_customers.json ./
COPY knowledge_docs/ ./knowledge_docs/

# --- Port --------------------------------------------------------------------
EXPOSE 8000

# --- Health check ------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# --- Run command -------------------------------------------------------------
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
