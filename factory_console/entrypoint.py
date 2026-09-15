"""Gunicorn import target; it intentionally has no environment fallback."""

from .runtime import build_application


application = build_application()
