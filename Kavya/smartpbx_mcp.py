"""Fail-closed Dialog MCP call control for Kavya SmartPBX sessions."""

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

_SDK_LOGGERS = ("mcp.client.streamable_http", "client")


class TransferDisabled(RuntimeError):
    """The explicitly configured handover boundary is unavailable."""


class MCPResponseTooLarge(httpx.TransportError):
    """The MCP server exceeded the configured response size limit."""


class MCPUnsupportedContentEncoding(httpx.TransportError):
    """The MCP server used an encoded response that cannot be bounded safely."""


@dataclass(frozen=True)
class TransferResult:
    """A bounded, non-sensitive classification of a transfer attempt."""

    transferred: bool
    failure: str | None = None


class _MCPSession(Protocol):
    async def initialize(self) -> object: ...

    async def call_tool(self, name: str, arguments: dict[str, str]) -> object: ...


SessionFactory = Callable[..., AsyncContextManager[_MCPSession]]


@dataclass(frozen=True)
class DialogMCPSettings:
    """Validated operator configuration; invalid and partial input is disabled."""

    endpoint: str = ""
    api_key: str = field(default="", repr=False)
    account_id: str = field(default="", repr=False)
    account_header: str = ""
    transfer_destinations: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )
    connect_timeout_seconds: int = 5
    read_timeout_seconds: int = 15
    max_response_bytes: int = 1048576
    retries: int = 1

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> DialogMCPSettings:
        """Load only a complete, safe configuration; otherwise disable transfer."""
        try:
            endpoint = _env_text(environ, "SMARTPBX_MCP_URL")
            api_key = _env_text(environ, "SMARTPBX_API_KEY")
            account_id = _env_text(environ, "SMARTPBX_ACCOUNT_ID")
            account_header = _env_text(environ, "SMARTPBX_MCP_ACCOUNT_HEADER")
            raw_destinations = _env_text(
                environ, "SMARTPBX_TRANSFER_DESTINATIONS_JSON"
            )
            values = (endpoint, api_key, account_id, account_header, raw_destinations)
            if not any(values):
                return cls()
            if not all(values):
                return cls()
            destinations = _parse_destinations(raw_destinations)
            connect_timeout = _bounded_integer(
                environ,
                "SMARTPBX_MCP_CONNECT_TIMEOUT_SECONDS",
                default=5,
                minimum=1,
                maximum=30,
            )
            read_timeout = _bounded_integer(
                environ,
                "SMARTPBX_MCP_READ_TIMEOUT_SECONDS",
                default=15,
                minimum=1,
                maximum=60,
            )
            max_response_bytes = _bounded_integer(
                environ,
                "SMARTPBX_MCP_MAX_RESPONSE_BYTES",
                default=1048576,
                minimum=128,
                maximum=1048576,
            )
            retries = _bounded_integer(
                environ,
                "SMARTPBX_MCP_RETRIES",
                default=1,
                minimum=0,
                maximum=1,
            )
            if (
                not _safe_header_value(api_key, _MAX_API_KEY_CHARS)
                or not _safe_header_value(account_id, _MAX_ACCOUNT_ID_CHARS)
                or account_header not in _ACCOUNT_HEADERS
            ):
                return cls()
            _validate_endpoint(endpoint)
        except (TypeError, ValueError, json.JSONDecodeError):
            return cls()
        return cls(
            endpoint=endpoint,
            api_key=api_key,
            account_id=account_id,
            account_header=account_header,
            transfer_destinations=MappingProxyType(destinations),
            connect_timeout_seconds=connect_timeout,
            read_timeout_seconds=read_timeout,
            max_response_bytes=max_response_bytes,
            retries=retries,
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.endpoint
            and self.api_key
            and self.account_id
            and self.account_header in _ACCOUNT_HEADERS
            and self.transfer_destinations
        )


class DialogMCPCallControl:
    """Expose one operator-allowlisted Dialog transfer operation."""

    def __init__(
        self,
        settings: DialogMCPSettings,
        context: CallContext,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._settings = settings
        self._context = context
        self._session_factory = session_factory or _open_session
        self._enabled = (
            settings.enabled
            and context.account_id == settings.account_id
            and _safe_call_id(context.other_leg_call_id)
        )

    async def transfer_call(self, destination_key: str) -> TransferResult:
        """Transfer only to a configured logical destination using the Dialog leg."""
        if not self._enabled or not isinstance(destination_key, str):
            raise TransferDisabled("handover is unavailable")
        destination = self._settings.transfer_destinations.get(destination_key)
        if destination is None:
            raise TransferDisabled("handover is unavailable")
        return await self._invoke("transfer_call", {"destination_number": destination})

    async def _invoke(self, tool_name: str, arguments: dict[str, str]) -> TransferResult:
        for attempt in range(1, self._settings.retries + 2):
            tool_dispatched = False
            try:
                async with asyncio.timeout(self._settings.read_timeout_seconds):
                    async with self._session_factory(
                        endpoint=self._settings.endpoint,
                        headers=self._headers(),
                        connect_timeout=self._settings.connect_timeout_seconds,
                        read_timeout=self._settings.read_timeout_seconds,
                        max_response_bytes=self._settings.max_response_bytes,
                    ) as session:
                        await session.initialize()
                        tool_dispatched = True
                        result = await session.call_tool(tool_name, arguments=arguments)
            except Exception as error:
                if (
                    not tool_dispatched
                    and attempt <= self._settings.retries
                    and _is_retryable_pre_dispatch(error)
                ):
                    _log_result("retryable_failure", attempt)
                    continue
                failure = _failure_class(error)
                _log_result(failure, attempt)
                return TransferResult(False, failure)

            outcome = _result_outcome(result)
            _log_result(outcome, attempt)
            return TransferResult(outcome == "success", None if outcome == "success" else outcome)
        return TransferResult(False, "request_failed")

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._settings.api_key,
            self._settings.account_header: self._settings.account_id,
            "call_id": self._context.other_leg_call_id,
        }


class _DropSDKLogRecords(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


_SDK_LOG_FILTER = _DropSDKLogRecords()


def _apply_sdk_logger_policy() -> None:
    """Prevent SDK request/response logging from exposing call data."""
    for logger_name in _SDK_LOGGERS:
        sdk_logger = logging.getLogger(logger_name)
        if _SDK_LOG_FILTER not in sdk_logger.filters:
            sdk_logger.addFilter(_SDK_LOG_FILTER)


class _BoundedResponseStream(httpx.AsyncByteStream):
    def __init__(self, source: httpx.AsyncByteStream, maximum_bytes: int) -> None:
        self._source = source
        self._maximum_bytes = maximum_bytes
        self._seen_bytes = 0
        self._closed = False

    async def __aiter__(self):
        async for chunk in self._source:
            self._seen_bytes += len(chunk)
            if self._seen_bytes > self._maximum_bytes:
                await self.aclose()
                raise MCPResponseTooLarge("SmartPBX MCP response exceeded byte limit")
            yield chunk

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._source.aclose()


class _BoundedHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, source: httpx.AsyncBaseTransport, maximum_response_bytes: int
    ) -> None:
        self._source = source
        self._maximum_response_bytes = maximum_response_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._source.handle_async_request(request)
        content_encoding = response.headers.get("Content-Encoding")
        if content_encoding is not None and content_encoding.strip().casefold() != "identity":
            await response.aclose()
            raise MCPUnsupportedContentEncoding("SmartPBX MCP response encoding rejected")
        declared_length = response.headers.get("Content-Length", "").strip()
        if declared_length.isdecimal() and int(declared_length) > self._maximum_response_bytes:
            await response.aclose()
            raise MCPResponseTooLarge("SmartPBX MCP response exceeded byte limit")
        response.stream = _BoundedResponseStream(
            response.stream, self._maximum_response_bytes
        )
        return response

    async def aclose(self) -> None:
        await self._source.aclose()


@asynccontextmanager
async def _open_session(
    *,
    endpoint: str,
    headers: Mapping[str, str],
    connect_timeout: int,
    read_timeout: int,
    max_response_bytes: int,
):
    """Open the official MCP Streamable HTTP client with bounded IO."""
    _apply_sdk_logger_policy()
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
    transport = _BoundedHTTPTransport(httpx.AsyncHTTPTransport(), max_response_bytes)
    client_headers = dict(headers)
    client_headers["Accept-Encoding"] = "identity"
    async with httpx.AsyncClient(
        headers=client_headers,
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
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
    return "tool_error" if is_error else "success"


def _is_retryable_pre_dispatch(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return 500 <= error.response.status_code <= 599
    return isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


def _failure_class(error: Exception) -> str:
    """Return only stable, non-sensitive failure classifications."""
    if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        return "server" if error.response.status_code >= 500 else "transport"
    if isinstance(error, httpx.TransportError):
        return "transport"
    return "transport"


def _log_result(outcome: str, attempt: int) -> None:
    logger.info("smartpbx_mcp_result outcome=%s attempt=%d", outcome, attempt)


def _env_text(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not isinstance(value, str):
        raise ValueError(name)
    return value.strip()


def _bounded_integer(
    environ: Mapping[str, str], name: str, *, default: int, minimum: int, maximum: int
) -> int:
    raw = environ.get(name, str(default))
    if not isinstance(raw, str) or not raw.isdecimal():
        raise ValueError(name)
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(name)
    return value


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        len(endpoint) > _MAX_ENDPOINT_CHARS
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.port is not None and not 1 <= parsed.port <= 65535)
    ):
        raise ValueError("endpoint")


def _parse_destinations(raw: str) -> dict[str, str]:
    if len(raw) > _MAX_DESTINATIONS_JSON_CHARS:
        raise ValueError("destinations")
    parsed = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(parsed, dict) or not parsed or len(parsed) > _MAX_DESTINATIONS:
        raise ValueError("destinations")
    destinations: dict[str, str] = {}
    for key, value in parsed.items():
        if (
            not isinstance(key, str)
            or _DESTINATION_KEY.fullmatch(key) is None
            or not isinstance(value, str)
            or len(value) > _MAX_DESTINATION_CHARS
            or (_TEL_DESTINATION.fullmatch(value) is None and _SIP_DESTINATION.fullmatch(value) is None)
        ):
            raise ValueError("destinations")
        if value.startswith("sip:") and ":" in value.rsplit("@", 1)[1]:
            port = int(value.rsplit(":", 1)[1])
            if not 1 <= port <= 65535:
                raise ValueError("destinations")
        destinations[key] = value
    return destinations


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("destinations")
        result[key] = value
    return result


def _safe_header_value(value: str, maximum_chars: int) -> bool:
    return bool(value) and len(value) <= maximum_chars and all(33 <= ord(char) <= 126 for char in value)


def _safe_call_id(value: object) -> bool:
    return isinstance(value, str) and _safe_header_value(value, _MAX_CALL_ID_CHARS)
