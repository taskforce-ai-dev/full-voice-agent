"""Dependency-free security primitives for the review-only website-demo profile.

The HTTP/WebSocket adapter owns framework and Twilio SDK integration.  Keeping
the ticket, quota, and TwiML construction here makes their safety contract
testable without production-only dependencies.
"""

from __future__ import annotations

import secrets
import time
import xml.sax.saxutils
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode, urlsplit


class WebsiteDemoConfigurationError(ValueError):
    """Raised for a website-demo setting that cannot be safely used."""


def validate_public_base_url(value: str) -> str:
    """Accept one canonical HTTPS origin; never derive a callback from Host."""
    if not isinstance(value, str):
        raise WebsiteDemoConfigurationError("website demo public URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise WebsiteDemoConfigurationError("website demo public URL must be an HTTPS origin")
    if parsed.port not in {None, 443}:
        raise WebsiteDemoConfigurationError("website demo public URL must use port 443")
    return f"https://{parsed.hostname.lower()}"


class TokenQuota:
    """Small process-local rate limiter with a hard client-state bound."""

    def __init__(
        self,
        *,
        limit: int = 5,
        window_seconds: float = 60.0,
        capacity: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or capacity < 1:
            raise ValueError("invalid website demo token quota")
        self._limit = limit
        self._window_seconds = window_seconds
        self._capacity = capacity
        self._clock = clock
        self._hits: OrderedDict[str, list[float]] = OrderedDict()

    @property
    def client_count(self) -> int:
        return len(self._hits)

    def admit(self, client: str) -> bool:
        if not isinstance(client, str) or not client or len(client) > 128:
            return False
        now = self._clock()
        hits = [item for item in self._hits.pop(client, []) if item > now - self._window_seconds]
        if len(hits) >= self._limit:
            self._hits[client] = hits
            return False
        hits.append(now)
        self._hits[client] = hits
        while len(self._hits) > self._capacity:
            self._hits.popitem(last=False)
        return True


@dataclass(frozen=True)
class _IssuedBrowserIdentity:
    expires_at: float


class IssuedBrowserIdentities:
    """Bounded, one-time browser identities accepted by the signed callback.

    A Twilio AccessToken's ``identity`` becomes ``From=client:<identity>`` on
    the voice webhook.  Recording it only after issuing a browser token keeps
    a signed callback from minting a relay ticket for an arbitrary client name.
    """

    def __init__(
        self,
        *,
        capacity: int = 512,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or ttl_seconds <= 0:
            raise ValueError("invalid website demo browser identity store")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._identities: OrderedDict[str, _IssuedBrowserIdentity] = OrderedDict()

    def issue(self, identity: str) -> None:
        if not _safe_identifier(identity):
            raise ValueError("invalid website demo browser identity")
        self._purge_expired()
        self._identities[identity] = _IssuedBrowserIdentity(self._clock() + self._ttl_seconds)
        self._identities.move_to_end(identity)
        while len(self._identities) > self._capacity:
            self._identities.popitem(last=False)

    def consume(self, identity: str) -> bool:
        self._purge_expired()
        return self._identities.pop(identity, None) is not None

    def _purge_expired(self) -> None:
        now = self._clock()
        for identity, record in tuple(self._identities.items()):
            if record.expires_at <= now:
                self._identities.pop(identity, None)


@dataclass(frozen=True)
class _SessionTicket:
    agent: str
    language: str
    expires_at: float


class SessionTickets:
    """One-time, short-lived relay admission tickets issued after signed TwiML."""

    def __init__(
        self,
        *,
        capacity: int = 128,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or ttl_seconds <= 0:
            raise ValueError("invalid website demo ticket store")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._tickets: OrderedDict[str, _SessionTicket] = OrderedDict()

    def issue(self, *, agent: str, language: str) -> str:
        if not _safe_identifier(agent) or not _safe_identifier(language):
            raise ValueError("invalid website demo ticket binding")
        self._purge_expired()
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = _SessionTicket(agent, language, self._clock() + self._ttl_seconds)
        while len(self._tickets) > self._capacity:
            self._tickets.popitem(last=False)
        return ticket

    def consume(self, ticket: str, *, agent: str, language: str) -> str | None:
        self._purge_expired()
        record = self._tickets.get(ticket)
        if record is None or record.agent != agent or record.language != language:
            return None
        self._tickets.pop(ticket, None)
        return record.language

    def _purge_expired(self) -> None:
        now = self._clock()
        for ticket, record in tuple(self._tickets.items()):
            if record.expires_at <= now:
                self._tickets.pop(ticket, None)


def conversation_relay_twiml(
    *, public_base_url: str, agent: str, language: str, locale: str, ticket: str, greeting: str
) -> str:
    """Build the inquiry-only text relay; it never exposes a dial/transfer path."""
    origin = validate_public_base_url(public_base_url)
    if not _safe_identifier(agent) or not _safe_identifier(language) or not _safe_locale(locale) or not ticket:
        raise WebsiteDemoConfigurationError("website demo relay binding is invalid")
    query = urlencode({"agent": agent, "lang": language, "ticket": ticket})
    websocket_url = f"wss://{urlsplit(origin).hostname}/ws/v1/website-demo/conversation?{query}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response><Connect>"
        f'<ConversationRelay url="{xml.sax.saxutils.escape(websocket_url, {chr(34): "&quot;"})}" '
        f'language="{xml.sax.saxutils.escape(locale, {chr(34): "&quot;"})}" '
        f'welcomeGreeting="{xml.sax.saxutils.escape(greeting, {chr(34): "&quot;"})}" '
        'interruptible="true" />'
        "</Connect></Response>"
    )


def _safe_identifier(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 80
        and all(character.isascii() and (character.isalnum() or character in "-_") for character in value)
    )


def _safe_locale(value: str) -> bool:
    return (
        isinstance(value, str)
        and 2 <= len(value) <= 35
        and all(character.isascii() and (character.isalnum() or character == "-") for character in value)
    )
