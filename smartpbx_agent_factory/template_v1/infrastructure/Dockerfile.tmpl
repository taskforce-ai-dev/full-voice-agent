# REVIEW-ONLY runtime container candidate. Rendering and release remain blocked.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# The lock is the only dependency input consumed by the image build.
COPY requirements-prod.lock.txt ./
RUN pip install --no-cache-dir -r requirements-prod.lock.txt

# Explicit rendered-output allowlist: no source-tree wildcard is permitted.
COPY startup.py server.py smartpbx_gateway.py smartpbx_protocol.py ./
COPY smartpbx_session.py smartpbx_transport.py smartpbx_diagnostics.py ./
COPY product_profile.py provider_adapters.py provider_runtime.py provider_builders.py turn_engine.py ./
COPY stt_adapters.py llm_adapters.py tts_adapters.py ./
COPY website_demo.py website_demo_core.py ./
COPY config/product_profile.json config/provider_profile.json ./config/
COPY knowledge_docs/ ./knowledge_docs/

# The import guard follows the ASGI module used by CMD. Build-only values stay
# inside this layer and are never written to ENV or the resulting image.
RUN SMARTPBX_WS_TOKEN=build-check-token SMARTPBX_ACCOUNT_ID=build-check-account \
    SMARTPBX_AUTH_HEADER_NAME=X-Build-Check-Token SMARTPBX_PRODUCT_PROFILE_PATH=/app/config/product_profile.json \
    SMARTPBX_KNOWLEDGE_DIR=/app/knowledge_docs SMARTPBX_PROVIDER_PROFILE_PATH=/app/config/provider_profile.json \
    SMARTPBX_RUNTIME_MODE=synthetic SMARTPBX_ALLOW_SYNTHETIC_FOR_CI=1 \
    python -c "import startup"

RUN WEBSITE_DEMO_ENABLED=true WEBSITE_DEMO_AGENT_ID=build-check-agent \
    WEBSITE_DEMO_PUBLIC_BASE_URL=https://demo.example.invalid WEBSITE_DEMO_ALLOWED_ORIGIN=https://demo.example.invalid \
    WEBSITE_DEMO_PRODUCT_PROFILE_PATH=/app/config/product_profile.json WEBSITE_DEMO_PROVIDER_PROFILE_PATH=/app/config/provider_profile.json \
    WEBSITE_DEMO_KNOWLEDGE_DIR=/app/knowledge_docs TWILIO_ACCOUNT_SID=build-account TWILIO_AUTH_TOKEN=build-token \
    TWILIO_API_KEY_SID=build-key TWILIO_API_KEY_SECRET=build-secret TWILIO_TWIML_APP_SID=build-app \
    SMARTPBX_RUNTIME_MODE=synthetic SMARTPBX_ALLOW_SYNTHETIC_FOR_CI=1 \
    python -c "import website_demo"

EXPOSE 8000
EXPOSE 8081
CMD ["uvicorn", "startup:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--ws-max-size", "65536"]
