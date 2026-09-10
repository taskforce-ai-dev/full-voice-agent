"""Isolated browser-demo ingress with a deliberately unavailable media adapter.

The token response matches the existing Taskforce website issuer shape.  The
customer-specific ConversationRelay and Media Streams implementation is not
copied here: until a client-neutral transport adapter is approved, every call
route fails closed before it can admit media or invoke a provider.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.request_validator import RequestValidator


_LANGUAGES = frozenset(("en", "ar", "ru", "si"))
_MAX_FIELD_CHARS = 48


class WebsiteDemoTransportUnavailable(RuntimeError):
    """Raised until a client-neutral browser carrier adapter is approved."""


class WebsiteDemoTransportAdapter(Protocol):
    async def voice_response(self, *, agent_id: str, language: str) -> Response: ...

    async def handle_media(self, websocket: WebSocket) -> None: ...


class UnavailableWebsiteDemoTransport:
    """Safe default: provider, knowledge, and media paths are never entered."""

    async def voice_response(self, *, agent_id: str, language: str) -> Response:
        raise WebsiteDemoTransportUnavailable("client-neutral website media adapter is not approved")

    async def handle_media(self, websocket: WebSocket) -> None:
        await websocket.close(code=1013, reason="website demo transport unavailable")


def _required(environ: Mapping[str, str], *names: str) -> dict[str, str]:
    values = {name: environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError("missing website-demo configuration: " + ", ".join(missing))
    return values


def _origins(raw: str) -> tuple[str, ...]:
    values = tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())
    if not values or len(values) != len(set(values)):
        raise RuntimeError("WEBSITE_DEMO_ALLOWED_ORIGINS must contain unique HTTPS origins")
    for value in values:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise RuntimeError("WEBSITE_DEMO_ALLOWED_ORIGINS must contain HTTPS origins only")
    return values


def _positive_int(environ: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except ValueError as error:
        raise RuntimeError(name + " must be an integer") from error
    if not 1 <= value <= maximum:
        raise RuntimeError(name + " is outside its bounded range")
    return value


@dataclass(frozen=True)
class WebsiteDemoConfiguration:
    agent_id: str
    public_url: str
    allowed_origins: tuple[str, ...]
    account_sid: str
    api_key_sid: str
    api_key_secret: str
    auth_token: str
    twiml_app_sid: str
    product_profile_path: str
    knowledge_dir: str
    provider_profile_path: str
    max_active_sessions: int
    token_ttl_seconds: int

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "WebsiteDemoConfiguration":
        if environ.get("WEBSITE_DEMO_ENABLED") != "true":
            raise RuntimeError("WEBSITE_DEMO_ENABLED must be exactly true")
        names = (
            "WEBSITE_DEMO_AGENT_ID", "WEBSITE_DEMO_PUBLIC_URL", "WEBSITE_DEMO_ALLOWED_ORIGINS",
            "WEBSITE_DEMO_TWILIO_ACCOUNT_SID", "WEBSITE_DEMO_TWILIO_API_KEY_SID",
            "WEBSITE_DEMO_TWILIO_API_KEY_SECRET", "WEBSITE_DEMO_TWILIO_AUTH_TOKEN",
            "WEBSITE_DEMO_TWIML_APP_SID", "WEBSITE_DEMO_PRODUCT_PROFILE_PATH",
            "WEBSITE_DEMO_KNOWLEDGE_DIR", "WEBSITE_DEMO_PROVIDER_PROFILE_PATH",
        )
        values = _required(environ, *names)
        if not values["WEBSITE_DEMO_AGENT_ID"].replace("-", "").isalnum():
            raise RuntimeError("WEBSITE_DEMO_AGENT_ID is invalid")
        public_url = values["WEBSITE_DEMO_PUBLIC_URL"].rstrip("/")
        parsed = urlsplit(public_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise RuntimeError("WEBSITE_DEMO_PUBLIC_URL must be an HTTPS origin")
        return cls(
            agent_id=values["WEBSITE_DEMO_AGENT_ID"], public_url=public_url,
            allowed_origins=_origins(values["WEBSITE_DEMO_ALLOWED_ORIGINS"]),
            account_sid=values["WEBSITE_DEMO_TWILIO_ACCOUNT_SID"],
            api_key_sid=values["WEBSITE_DEMO_TWILIO_API_KEY_SID"],
            api_key_secret=values["WEBSITE_DEMO_TWILIO_API_KEY_SECRET"],
            auth_token=values["WEBSITE_DEMO_TWILIO_AUTH_TOKEN"],
            twiml_app_sid=values["WEBSITE_DEMO_TWIML_APP_SID"],
            product_profile_path=values["WEBSITE_DEMO_PRODUCT_PROFILE_PATH"],
            knowledge_dir=values["WEBSITE_DEMO_KNOWLEDGE_DIR"],
            provider_profile_path=values["WEBSITE_DEMO_PROVIDER_PROFILE_PATH"],
            max_active_sessions=_positive_int(environ, "WEBSITE_DEMO_MAX_ACTIVE_SESSIONS", 4, 16),
            token_ttl_seconds=_positive_int(environ, "WEBSITE_DEMO_TOKEN_TTL_SECONDS", 300, 600),
        )


@dataclass
class DemoTokenRegistry:
    capacity: int
    ttl_seconds: int
    _leases: dict[str, float] = field(default_factory=dict)
    _rate: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def _prune(self, now: float) -> None:
        self._leases = {identity: expiry for identity, expiry in self._leases.items() if expiry > now}

    def issue(self, client_ip: str) -> str:
        now = time.monotonic()
        self._prune(now)
        attempts = self._rate[client_ip]
        attempts[:] = [attempt for attempt in attempts if attempt > now - 60.0]
        if len(attempts) >= 5:
            raise HTTPException(status_code=429, detail="Too many token requests")
        if len(self._leases) >= self.capacity:
            raise HTTPException(status_code=429, detail="Demo capacity is reached")
        attempts.append(now)
        identity = "demo-" + uuid.uuid4().hex[:20]
        self._leases[identity] = now + self.ttl_seconds
        return identity

    def consume(self, identity: str) -> bool:
        self._prune(time.monotonic())
        expiry = self._leases.pop(identity, None)
        return expiry is not None


def _bounded(value: object) -> str:
    text = value if isinstance(value, str) else ""
    if len(text) > _MAX_FIELD_CHARS:
        raise HTTPException(status_code=400, detail="Invalid demo request")
    return text.strip()


async def _request_values(request: Request) -> dict[str, str]:
    values = {key: _bounded(value) for key, value in request.query_params.items()}
    if request.method == "POST":
        form = await request.form(max_files=0, max_fields=12, max_part_size=4096)
        values.update({key: _bounded(value) for key, value in form.items()})
    return values


def _requested_agent(values: Mapping[str, str], config: WebsiteDemoConfiguration) -> str:
    if not secrets.compare_digest(values.get("agent", ""), config.agent_id):
        raise HTTPException(status_code=404, detail="Unknown demo")
    return config.agent_id


def _requested_language(values: Mapping[str, str]) -> str:
    language = values.get("lang", "en").lower()
    if language not in _LANGUAGES:
        raise HTTPException(status_code=400, detail="Unsupported demo language")
    return language


def _verify_twilio_request(request: Request, values: Mapping[str, str], config: WebsiteDemoConfiguration) -> None:
    signature = request.headers.get("X-Twilio-Signature", "")
    url = config.public_url + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    if not signature or not RequestValidator(config.auth_token).validate(url, dict(values), signature):
        raise HTTPException(status_code=403, detail="Invalid voice request")


def build_website_demo_app(
    config: WebsiteDemoConfiguration, transport: WebsiteDemoTransportAdapter | None = None,
) -> FastAPI:
    app = FastAPI(title="Website demo", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(config.allowed_origins), allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"],
    )
    registry = DemoTokenRegistry(config.max_active_sessions, config.token_ttl_seconds)
    adapter = transport or UnavailableWebsiteDemoTransport()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service_mode": "website-demo", "transport": "unavailable"}

    @app.get("/api/voice-token")
    async def voice_token(request: Request) -> dict[str, str]:
        origin = request.headers.get("origin", "").rstrip("/")
        if origin not in config.allowed_origins:
            raise HTTPException(status_code=403, detail="Origin is not allowed")
        if isinstance(adapter, UnavailableWebsiteDemoTransport):
            raise HTTPException(status_code=503, detail="Website demo transport is not approved")
        _requested_agent(await _request_values(request), config)
        client_ip = request.client.host if request.client else "unknown"
        identity = registry.issue(client_ip)
        token = AccessToken(config.account_sid, config.api_key_sid, config.api_key_secret, identity=identity, ttl=config.token_ttl_seconds)
        token.add_grant(VoiceGrant(outgoing_application_sid=config.twiml_app_sid, incoming_allow=False))
        return {"token": token.to_jwt(), "identity": identity}

    @app.api_route("/voice/demo-incoming", methods=["GET", "POST"])
    async def voice_demo_incoming(request: Request) -> Response:
        values = await _request_values(request)
        _verify_twilio_request(request, values, config)
        agent_id = _requested_agent(values, config)
        language = _requested_language(values)
        caller = values.get("From", "")
        if not caller.startswith("client:") or not registry.consume(caller[7:]):
            raise HTTPException(status_code=403, detail="Demo session is not active")
        try:
            return await adapter.voice_response(agent_id=agent_id, language=language)
        except WebsiteDemoTransportUnavailable as error:
            raise HTTPException(status_code=503, detail="Website demo transport is not approved") from error

    @app.websocket("/ws/website-demo/media")
    async def website_demo_media(websocket: WebSocket) -> None:
        await adapter.handle_media(websocket)

    return app


app = build_website_demo_app(WebsiteDemoConfiguration.from_environ(os.environ))
