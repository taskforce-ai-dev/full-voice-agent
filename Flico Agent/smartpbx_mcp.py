"""Allowlisted Dialog SmartPBX call control over MCP Streamable HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import AsyncContextManager, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import httpx

from smartpbx_protocol import CallContext


logger = logging.getLogger(__name__)

_ACCOUNT_HEADERS = frozenset({"account_id", "X-Account-ID"})
_DESTINATION_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}\Z", re.ASCII)
_TEL_DESTINATION = re.compile(r"tel:\+[1-9][0-9]{6,14}\Z", re.ASCII)
_SIP_DESTINATION = re.compile(
    r"sip:[A-Za-z0-9_.+-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,188}[A-Za-z0-9])?(?::[0-9]{1,5})?\Z",
    re.ASCII,
)
_MAX_DESTINATIONS_JSON_CHARS = 4096
_MAX_DESTINATIONS = 16
_MAX_DESTINATION_CHARS = 256

_MAX_ENDPOINT_CHARS = 2048
_MAX_API_KEY_CHARS = 512
_MAX_ACCOUNT_ID_CHARS = 256
_MAX_CALL_ID_CHARS = 256

class CallControl(Protocol):
    """The only call-control operations available to the voice pipeline."""

    async def transfer_call(self, destination_key: str) -> bool: ...

    async def hangup_call(self) -> bool: ...


class _MCPSession(Protocol):
    async def initialize(self) -> object: ...

    async def call_tool(self, name: str, arguments: dict[str, str]) -> object: ...


SessionFactory = Callable[
    ...,
    AsyncContextManager[_MCPSession],
]


@dataclass(frozen=True)
class DialogMCPSettings:
    endpoint: str
    api_key: str = field(repr=False)
    expected_account_id: str = field(repr=False)
    account_header: str
    transfer_destinations: Mapping[str, str] = field(repr=False)
    connect_timeout_seconds: int
    read_timeout_seconds: int
    retry_count: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> DialogMCPSettings:
        """Parse fail-closed MCP configuration without defaulting the account header."""
        endpoint = _env_text(environ, "SMARTPBX_MCP_URL")
        api_key = _env_text(environ, "SMARTPBX_API_KEY")
        account_id = _env_text(environ, "SMARTPBX_ACCOUNT_ID")
        account_header = _env_text(environ, "SMARTPBX_MCP_ACCOUNT_HEADER")
        raw_destinations = _env_text(environ, "SMARTPBX_TRANSFER_DESTINATIONS_JSON")
        connect_timeout = _bounded_integer(
            environ, "SMARTPBX_MCP_CONNECT_TIMEOUT_SECONDS", default=5, minimum=1, maximum=30
        )
        read_timeout = _bounded_integer(
            environ, "SMARTPBX_MCP_READ_TIMEOUT_SECONDS", default=15, minimum=1, maximum=60
        )
        retry_count = _bounded_integer(
            environ, "SMARTPBX_MCP_RETRY_COUNT", default=1, minimum=0, maximum=2
        )

        attempted = any((endpoint, api_key, account_header, raw_destinations))
        destinations = _parse_destinations(raw_destinations)
        value = cls(
            endpoint=endpoint,
            api_key=api_key,
            expected_account_id=account_id,
            account_header=account_header,
            transfer_destinations=MappingProxyType(destinations),
            connect_timeout_seconds=connect_timeout,
            read_timeout_seconds=read_timeout,
            retry_count=retry_count,
        )
        if not attempted:
            return value
        if (
            not _safe_header_value(api_key, _MAX_API_KEY_CHARS)
            or not _safe_header_value(account_id, _MAX_ACCOUNT_ID_CHARS)
            or account_header not in _ACCOUNT_HEADERS
        ):
            raise _configuration_error()
        _validate_endpoint(endpoint)
        return value

    @property
    def configured(self) -> bool:
        return bool(
            self.endpoint
            and self.api_key
            and self.expected_account_id
            and self.account_header in _ACCOUNT_HEADERS
        )

    @property
    def enabled(self) -> bool:
        return self.configured


class DialogMCPCallControl:
    """Invoke only stable Dialog tools against one validated call context."""

    def __init__(
        self,
        settings: DialogMCPSettings,
        context: CallContext,
        session_factory: SessionFactory | None = None,
    ) -> None:
        if (
            not settings.configured
            or context.account_id != settings.expected_account_id
            or not isinstance(context.other_leg_call_id, str)
            or not _safe_header_value(context.other_leg_call_id, _MAX_CALL_ID_CHARS)
        ):
            raise ValueError("invalid SmartPBX MCP context")
        self._settings = settings
        self._context = context
        self._session_factory = session_factory or _open_session

    async def transfer_call(self, destination_key: str) -> bool:
        if not isinstance(destination_key, str):
            _log_result("transfer_call", "destination_rejected", 0)
            return False
        destination = self._settings.transfer_destinations.get(destination_key)
        if destination is None:
            _log_result("transfer_call", "destination_rejected", 0)
            return False
        return await self._invoke(
            "transfer_call", {"destination_number": destination}
        )

    async def hangup_call(self) -> bool:
        return await self._invoke("hangup_call", {})

    async def _invoke(self, tool_name: str, arguments: dict[str, str]) -> bool:
        attempts = self._settings.retry_count + 1
        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(self._settings.read_timeout_seconds):
                    async with self._session_factory(
                        endpoint=self._settings.endpoint,
                        headers=self._headers(),
                        connect_timeout=self._settings.connect_timeout_seconds,
                        read_timeout=self._settings.read_timeout_seconds,
                    ) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments=arguments)
            except Exception as error:
                retryable = _is_retryable(error)
                if retryable and attempt < attempts:
                    _log_result(tool_name, "retryable_failure", attempt)
                    continue
                _log_result(tool_name, "request_failed", attempt)
                return False

            outcome = _result_outcome(result)
            _log_result(tool_name, outcome, attempt)
            return outcome == "success"
        return False

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._settings.api_key,
            self._settings.account_header: self._context.account_id,
            "call_id": self._context.other_leg_call_id,
        }


@asynccontextmanager
async def _open_session(
    *, endpoint: str, headers: Mapping[str, str], connect_timeout: int, read_timeout: int
):
    """Open the official MCP v1 Streamable HTTP client lifecycle."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
    async with httpx.AsyncClient(
        headers=dict(headers), timeout=timeout, follow_redirects=False
    ) as http_client:
        async with streamable_http_client(endpoint, http_client=http_client) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                yield session


def _result_outcome(result: object) -> str:
    is_error = getattr(result, "isError", None)
    content = getattr(result, "content", None)
    if not isinstance(is_error, bool) or not isinstance(content, list) or not content:
        return "malformed_result"
    if is_error:
        return "tool_error"
    return "success"


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return 500 <= error.response.status_code <= 599
    return isinstance(error, (TimeoutError, httpx.TransportError))


def _log_result(tool_name: str, outcome: str, attempt: int) -> None:
    logger.info(
        "smartpbx_mcp_result event=smartpbx_mcp_result tool=%s outcome=%s attempt=%d",
        tool_name,
        outcome,
        attempt,
    )


def _validate_endpoint(endpoint: str) -> None:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise _configuration_error() from error
    if (
        len(endpoint) > _MAX_ENDPOINT_CHARS
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise _configuration_error()


def _parse_destinations(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    if len(raw) > _MAX_DESTINATIONS_JSON_CHARS:
        raise _configuration_error()
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise _configuration_error() from error
    if not isinstance(parsed, dict) or len(parsed) > _MAX_DESTINATIONS:
        raise _configuration_error()
    destinations: dict[str, str] = {}
    for key, value in parsed.items():
        if (
            not isinstance(key, str)
            or _DESTINATION_KEY.fullmatch(key) is None
            or not isinstance(value, str)
            or len(value) > _MAX_DESTINATION_CHARS
            or (
                _TEL_DESTINATION.fullmatch(value) is None
                and _SIP_DESTINATION.fullmatch(value) is None
            )
        ):
            raise _configuration_error()
        destinations[key] = value
        if value.startswith("sip:") and ":" in value.rsplit("@", 1)[1]:
            if int(value.rsplit(":", 1)[1]) > 65535:
                raise _configuration_error()
    return destinations


def _env_text(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not isinstance(value, str):
        raise _configuration_error()
    return value


def _bounded_integer(
    environ: Mapping[str, str], name: str, *, default: int, minimum: int, maximum: int
) -> int:
    raw = environ.get(name, str(default))
    if not isinstance(raw, str) or not raw.isdecimal():
        raise _configuration_error()
    value = int(raw)
    if not minimum <= value <= maximum:
        raise _configuration_error()
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _safe_header_value(value: str, max_chars: int) -> bool:
    return bool(value) and len(value) <= max_chars and all(
        33 <= ord(char) <= 126 for char in value
    )


def _configuration_error() -> ValueError:
    return ValueError("invalid SmartPBX MCP configuration")
