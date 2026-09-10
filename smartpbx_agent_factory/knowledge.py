"""Bounded, review-only ingestion for factory knowledge sources.

Source material is untrusted data.  This module extracts text for a human
review; it never interprets source text as instructions or executes it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from html.parser import HTMLParser
import http.client
import ipaddress
from io import BytesIO
import json
from pathlib import Path
import posixpath
import re
import socket
import ssl
from typing import Iterable, Protocol
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .model import KnowledgeSource
from .state import GenerationState


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TOTAL_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_CHARS = 200_000
DEFAULT_MAX_REDIRECTS = 8
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_HEADERS = 64
MAX_HEADER_NAME_CHARS = 128
MAX_HEADER_VALUE_CHARS = 4096
MAX_REDIRECT_LOCATION_CHARS = 4096

_LOCAL_SUFFIXES = frozenset({".pdf", ".docx", ".txt", ".md", ".markdown"})
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "text/markdown",
        "text/html",
    }
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()\-]{6,}\d)(?!\w)")
_INSTRUCTION = re.compile(
    r"(?im)^\s*(?:ignore|disregard|override|system\s+message|assistant\s*:|developer\s*:|"
    r"upload|exfiltrate)\b"
)
_ENCODED_SEPARATOR = re.compile(r"%(?:25)*(?:2f|5c)", re.IGNORECASE)


class KnowledgeError(ValueError):
    """Raised when a source violates the knowledge-ingestion boundary."""


class KnowledgeApprovalRequired(KnowledgeError):
    """Raised when generation tries to use a review without its exact approval."""


@dataclass(frozen=True)
class KnowledgeFact:
    text: str
    source_uri: str
    location: str


@dataclass(frozen=True)
class KnowledgeConflict:
    subject: str
    values: tuple[str, ...]
    source_locations: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeDocument:
    """Canonical, immutable source material covered by a knowledge review digest."""

    uri: str
    owner: str
    effective_date: str
    classification: str
    text: str


@dataclass(frozen=True)
class KnowledgeReview:
    facts: tuple[KnowledgeFact, ...]
    conflicts: tuple[KnowledgeConflict, ...]
    missing_facts: tuple[str, ...]
    sensitive_findings: tuple[str, ...]
    inaccessible_sources: tuple[str, ...]
    duplicate_facts: tuple[str, ...]
    instruction_findings: tuple[str, ...]
    digest: str
    documents: tuple[KnowledgeDocument, ...] = ()
    executed_instructions: bool = False

    def approval_status(self, state: GenerationState | None = None) -> str:
        if state is not None and state.knowledge_approval_digest == self.digest:
            return "approved"
        return "approval-required"

    def require_approved(self, approval: str | GenerationState) -> None:
        """Require the immutable review digest, optionally from generation state."""
        if isinstance(approval, GenerationState):
            expected = approval.knowledge_approval_digest
            if expected != self.digest:
                raise KnowledgeApprovalRequired("knowledge approval digest does not match this review")
            return
        if approval != self.digest:
            raise KnowledgeApprovalRequired("knowledge approval digest does not match this review")


def recompute_knowledge_review_digest(review: KnowledgeReview) -> str:
    """Recompute the canonical digest of one concrete immutable knowledge review."""
    if type(review) is not KnowledgeReview:
        raise KnowledgeError("knowledge review must be a concrete KnowledgeReview")
    documents = _require_tuple_of(review.documents, KnowledgeDocument, "knowledge review documents")
    facts = _require_tuple_of(review.facts, KnowledgeFact, "knowledge review facts")
    conflicts = _require_tuple_of(review.conflicts, KnowledgeConflict, "knowledge review conflicts")
    missing = _require_string_tuple(review.missing_facts, "knowledge review missing facts")
    sensitive = _require_string_tuple(review.sensitive_findings, "knowledge review sensitive findings")
    inaccessible = _require_string_tuple(review.inaccessible_sources, "knowledge review inaccessible sources")
    duplicates = _require_string_tuple(review.duplicate_facts, "knowledge review duplicate facts")
    instructions = _require_string_tuple(review.instruction_findings, "knowledge review instruction findings")
    for document in documents:
        _require_dataclass_strings(document, "knowledge document")
    for fact in facts:
        _require_dataclass_strings(fact, "knowledge fact")
    for conflict in conflicts:
        if not isinstance(conflict.subject, str):
            raise KnowledgeError("knowledge conflict subject must be text")
        _require_string_tuple(conflict.values, "knowledge conflict values")
        _require_string_tuple(conflict.source_locations, "knowledge conflict source locations")
    payload = {
        "sources": [
            {
                "uri": document.uri,
                "owner": document.owner,
                "effective_date": document.effective_date,
                "classification": document.classification,
                "text": document.text,
            }
            for document in documents
        ],
        "facts": [fact.__dict__ for fact in facts],
        "conflicts": [conflict.__dict__ for conflict in conflicts],
        "missing_facts": missing,
        "sensitive_findings": sensitive,
        "inaccessible_sources": inaccessible,
        "duplicate_facts": duplicates,
        "instruction_findings": instructions,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _require_tuple_of(value: object, expected: type[object], field_name: str) -> tuple[object, ...]:
    if not isinstance(value, tuple) or any(type(item) is not expected for item in value):
        raise KnowledgeError(f"{field_name} must be an immutable tuple of {expected.__name__} values")
    return value


def _require_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise KnowledgeError(f"{field_name} must be an immutable tuple of text values")
    return value


def _require_dataclass_strings(value: object, field_name: str) -> None:
    if not all(isinstance(item, str) for item in value.__dict__.values()):
        raise KnowledgeError(f"{field_name} fields must be text")


class KnowledgeBuilder(Protocol):
    def build(self, sources: tuple[KnowledgeSource, ...], output_dir: Path) -> KnowledgeReview:
        """Extract bounded source data and return the human-review artifact."""


class URLNetworkPolicy(Protocol):
    """Network destinations permitted after manifest-origin validation."""

    def validate(self, hostname: str) -> None:
        """Reject a destination hostname that is unsafe for this environment."""


class URLResolver(Protocol):
    """Resolve a hostname once before a pinned connection is made."""

    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        """Return every candidate A/AAAA address for the destination."""


@dataclass(frozen=True)
class URLFetchResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class URLTransport(Protocol):
    """Fetch a URL by connecting only to already-validated IP addresses."""

    def fetch(
        self, url: str, addresses: tuple[str, ...], timeout_seconds: float, max_bytes: int
    ) -> URLFetchResponse:
        """Fetch through the supplied address set without resolving the hostname again."""


class _PublicURLNetworkPolicy:
    def validate(self, hostname: str) -> None:
        if hostname.lower().rstrip(".") == "localhost":
            raise KnowledgeError("loopback URL origins are not allowed")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return
        if address.is_loopback or address.is_private or address.is_link_local:
            raise KnowledgeError("private, link-local, or loopback URL origins are not allowed")


class _SystemURLResolver:
    def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        addresses = tuple(
            dict.fromkeys(
                item[4][0]
                for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
                if item[0] in {socket.AF_INET, socket.AF_INET6}
            )
        )
        if not addresses:
            raise OSError("hostname did not resolve to an IP address")
        return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, address: str, port: int, server_hostname: str, timeout_seconds: float) -> None:
        super().__init__(address, port=port, timeout=timeout_seconds)
        self._server_hostname = server_hostname

    def connect(self) -> None:
        self.sock = self._create_connection((self.host, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname)


class _PinnedURLTransport:
    def fetch(
        self, url: str, addresses: tuple[str, ...], timeout_seconds: float, max_bytes: int
    ) -> URLFetchResponse:
        parsed = urlsplit(url)
        if not parsed.hostname:
            raise KnowledgeError("URL must have a hostname")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise KnowledgeError("URL origin has an invalid port") from exc
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        authority = parsed.hostname
        if ":" in authority:
            authority = f"[{authority}]"
        if parsed.port not in {None, 443 if parsed.scheme == "https" else 80}:
            authority = f"{authority}:{parsed.port}"
        last_error: OSError | None = None
        for address in addresses:
            connection: http.client.HTTPConnection
            if parsed.scheme == "https":
                connection = _PinnedHTTPSConnection(address, port, parsed.hostname, timeout_seconds)
            else:
                connection = http.client.HTTPConnection(address, port=port, timeout=timeout_seconds)
            try:
                connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
                connection.putheader("Host", authority)
                connection.putheader("Accept", ", ".join(sorted(_ALLOWED_CONTENT_TYPES)))
                connection.endheaders()
                response = connection.getresponse()
                return URLFetchResponse(
                    status=response.status,
                    headers={key.lower(): value for key, value in response.getheaders()},
                    body=response.read(max_bytes + 1),
                )
            except (OSError, http.client.HTTPException) as exc:
                last_error = OSError(str(exc))
            finally:
                connection.close()
        raise last_error or OSError("pinned connection failed")


@dataclass(frozen=True)
class _ExtractedSource:
    source: KnowledgeSource
    uri: str
    text: str
    byte_count: int
    markdown: bool


class _SourceUnavailable(Exception):
    pass


class _TextOnlyHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return "\n".join(self.parts)


class KnowledgeBuilderImpl:
    """Extract source text under explicit local and network resource bounds."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        total_max_bytes: int = DEFAULT_TOTAL_MAX_BYTES,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        approved_source_roots: Iterable[Path] | None = None,
        network_policy: URLNetworkPolicy | None = None,
        resolver: URLResolver | None = None,
        transport: URLTransport | None = None,
    ) -> None:
        if min(max_bytes, total_max_bytes, max_chars, max_redirects) <= 0 or timeout_seconds <= 0:
            raise KnowledgeError("knowledge limits must be positive")
        self.max_bytes = max_bytes
        self.total_max_bytes = total_max_bytes
        self.max_chars = max_chars
        self.max_redirects = max_redirects
        self.timeout_seconds = timeout_seconds
        self.approved_source_roots = tuple(Path(root) for root in approved_source_roots or ())
        self.network_policy = network_policy or _PublicURLNetworkPolicy()
        self.resolver = resolver or _SystemURLResolver()
        self.transport = transport or _PinnedURLTransport()

    def build(self, sources: tuple[KnowledgeSource, ...], output_dir: Path) -> KnowledgeReview:
        if not isinstance(sources, tuple):
            raise KnowledgeError("knowledge sources must be an immutable tuple")
        total_bytes = 0
        extracted: list[_ExtractedSource] = []
        inaccessible: list[str] = []
        for source in sources:
            try:
                item = self._extract_source(source)
            except _SourceUnavailable:
                inaccessible.append(self._sanitized_source_uri(source))
                continue
            total_bytes += item.byte_count
            if total_bytes > self.total_max_bytes:
                raise KnowledgeError("total source byte limit exceeded")
            extracted.append(item)

        facts, duplicates = self._facts(extracted)
        conflicts = self._conflicts(facts)
        missing = self._missing_metadata(extracted)
        sensitive = self._sensitive_findings(extracted)
        instructions = (
            ("source text contains instruction-like content",)
            if any(_INSTRUCTION.search(item.text) for item in extracted)
            else ()
        )
        review = KnowledgeReview(
            facts=tuple(facts),
            conflicts=tuple(conflicts),
            missing_facts=missing,
            sensitive_findings=sensitive,
            inaccessible_sources=tuple(inaccessible),
            duplicate_facts=tuple(duplicates),
            instruction_findings=instructions,
            digest="",
            documents=tuple(
                KnowledgeDocument(
                    uri=item.uri,
                    owner=item.source.owner,
                    effective_date=item.source.effective_date,
                    classification=item.source.classification,
                    text=item.text,
                )
                for item in extracted
            ),
        )
        review = replace(review, digest=recompute_knowledge_review_digest(review))
        self._write_review(extracted, review, Path(output_dir))
        return review

    def _extract_source(self, source: KnowledgeSource) -> _ExtractedSource:
        if not isinstance(source, KnowledgeSource):
            raise KnowledgeError("knowledge source has an invalid type")
        if source.kind == "local":
            if not source.path or source.url:
                raise KnowledgeError("local knowledge source requires path only")
            path = self._safe_local_path(source.path)
            content = self._read_bounded(path)
            text = self._normalize_text(self._extract_bytes(content, path.suffix.lower(), None))
            return _ExtractedSource(
                source, path.as_uri(), text, len(content), path.suffix.lower() in {".md", ".markdown"}
            )
        if source.kind == "url":
            if not source.url or source.path:
                raise KnowledgeError("URL knowledge source requires url only")
            content, content_type, effective_url = self._fetch_url(source)
            suffix = Path(urlsplit(effective_url).path).suffix.lower()
            text = self._normalize_text(self._extract_bytes(content, suffix, content_type))
            return _ExtractedSource(
                source,
                self._sanitize_url(effective_url),
                text,
                len(content),
                suffix in {".md", ".markdown"} or content_type == "text/markdown",
            )
        raise KnowledgeError("knowledge source kind must be local or url")

    def _safe_local_path(self, value: str) -> Path:
        raw = Path(value).expanduser()
        if "\x00" in value or any(part == ".." for part in raw.parts):
            raise KnowledgeError("local knowledge source is outside approved root")
        roots = self.approved_source_roots
        if not roots:
            raise KnowledgeError("local knowledge source requires explicit approved source roots")
        candidates: tuple[tuple[Path, Path], ...]
        if raw.is_absolute():
            candidates = tuple((Path(root), raw) for root in roots)
        else:
            candidates = tuple((Path(root), Path(root) / raw) for root in roots)
        for root, candidate in candidates:
            try:
                return self._contained_regular_file(root, candidate)
            except KnowledgeError:
                continue
        raise KnowledgeError("local knowledge source is outside approved root or contains a symlink")

    @staticmethod
    def _contained_regular_file(root: Path, candidate: Path) -> Path:
        try:
            root_absolute = root.expanduser().absolute()
            candidate_absolute = candidate.expanduser().absolute()
            root_real = root_absolute.resolve(strict=True)
            candidate_real = candidate_absolute.resolve(strict=True)
        except FileNotFoundError as exc:
            raise KnowledgeError("local knowledge source does not exist") from exc
        if root_absolute.is_symlink():
            raise KnowledgeError("approved source root must not be a symlink")
        try:
            relative = candidate_absolute.relative_to(root_absolute)
        except ValueError as exc:
            raise KnowledgeError("local knowledge source is outside approved root") from exc
        current = root_absolute
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise KnowledgeError("local knowledge source symlink is not allowed")
        if candidate_real != root_real and root_real not in candidate_real.parents:
            raise KnowledgeError("local knowledge source realpath is outside approved root")
        if not candidate_real.is_file():
            raise KnowledgeError("local knowledge source must be a regular file")
        return candidate_absolute

    def _read_bounded(self, path: Path) -> bytes:
        with path.open("rb") as handle:
            content = handle.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise KnowledgeError("source byte limit exceeded")
        return content

    def _fetch_url(self, source: KnowledgeSource) -> tuple[bytes, str, str]:
        current_url = source.url or ""
        for redirects in range(self.max_redirects + 1):
            self._check_allowed_url(current_url, source)
            addresses = self._resolve_global_addresses(current_url)
            try:
                response = self.transport.fetch(
                    current_url, addresses, self.timeout_seconds, self.max_bytes
                )
            except KnowledgeError:
                raise
            except (OSError, TimeoutError) as exc:
                raise _SourceUnavailable from exc
            status, headers, body = self._validate_fetch_response(response)
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    raise _SourceUnavailable
                if len(location) > MAX_REDIRECT_LOCATION_CHARS:
                    raise KnowledgeError("redirect location header is too large")
                if redirects == self.max_redirects:
                    raise KnowledgeError("URL redirect limit exceeded")
                current_url = urljoin(current_url, location)
                continue
            if status < 200 or status >= 300:
                raise _SourceUnavailable
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in _ALLOWED_CONTENT_TYPES:
                raise KnowledgeError("URL content type is not allowed")
            declared = headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
                raise KnowledgeError("source byte limit exceeded")
            return body, content_type, current_url
        raise KnowledgeError("URL redirect limit exceeded")

    def _validate_fetch_response(self, response: object) -> tuple[int, dict[str, str], bytes]:
        if not isinstance(response, URLFetchResponse):
            raise KnowledgeError("transport returned an invalid response")
        if type(response.status) is not int or not 100 <= response.status <= 599:
            raise KnowledgeError("transport response status is invalid")
        if not isinstance(response.headers, dict) or len(response.headers) > MAX_RESPONSE_HEADERS:
            raise KnowledgeError("transport response headers are invalid")
        headers: dict[str, str] = {}
        for name, value in response.headers.items():
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or not name
                or len(name) > MAX_HEADER_NAME_CHARS
                or len(value) > MAX_HEADER_VALUE_CHARS
                or "\r" in name
                or "\n" in name
                or "\r" in value
                or "\n" in value
            ):
                raise KnowledgeError("transport response header is invalid")
            normalized = name.lower()
            if normalized in headers:
                raise KnowledgeError("transport response has duplicate headers")
            headers[normalized] = value
        if not isinstance(response.body, bytes) or len(response.body) > self.max_bytes:
            raise KnowledgeError("transport response body is invalid or exceeds byte limit")
        return response.status, headers, response.body

    def _resolve_global_addresses(self, value: str) -> tuple[str, ...]:
        parsed = urlsplit(value)
        if not parsed.hostname:
            raise KnowledgeError("URL must have a hostname")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise KnowledgeError("URL origin has an invalid port") from exc
        try:
            addresses = self.resolver.resolve(parsed.hostname, port)
        except OSError as exc:
            raise _SourceUnavailable from exc
        if not addresses:
            raise _SourceUnavailable
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as exc:
                raise KnowledgeError("resolver returned an invalid IP address") from exc
            if not parsed_address.is_global:
                raise KnowledgeError("resolved URL address is non-global")
        return addresses

    @staticmethod
    def _normal_path(value: str) -> str:
        path = unquote(value or "/")
        return "/" + posixpath.normpath("/" + path).lstrip("/")

    @staticmethod
    def _reject_ambiguous_encoded_path(value: str) -> None:
        decoded = value
        for _ in range(4):
            if _ENCODED_SEPARATOR.search(decoded):
                raise KnowledgeError("ambiguous percent-encoded URL separator")
            next_value = unquote(decoded)
            if "\x00" in next_value or "\\" in next_value:
                raise KnowledgeError("ambiguous percent-encoded URL path")
            if any(part in {".", ".."} for part in next_value.split("/")):
                raise KnowledgeError("ambiguous percent-encoded URL traversal")
            if next_value == decoded:
                if "%" in decoded:
                    raise KnowledgeError("ambiguous percent-encoded URL path")
                return
            decoded = next_value
        raise KnowledgeError("ambiguous percent-encoded URL path")

    def _check_allowed_url(self, value: str, source: KnowledgeSource) -> None:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise KnowledgeError("URL must have an HTTP(S) origin")
        if parsed.username or parsed.password:
            raise KnowledgeError("URL credentials are not allowed")
        self.network_policy.validate(parsed.hostname)
        origin = self._canonical_origin(value)
        allowed_origins = {self._canonical_origin(item, approved=True) for item in source.approved_origins}
        if origin not in allowed_origins:
            raise KnowledgeError("URL origin is not allowlisted")
        self._reject_ambiguous_encoded_path(parsed.path)
        path = self._normal_path(parsed.path)
        if source.path_prefixes and not any(self._path_matches(path, prefix) for prefix in source.path_prefixes):
            raise KnowledgeError("URL path is outside the allowlisted prefix")

    @staticmethod
    def _canonical_origin(value: str, *, approved: bool = False) -> tuple[str, str, int]:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise KnowledgeError("approved URL origin is invalid")
        if parsed.username or parsed.password:
            raise KnowledgeError("approved URL origin must not contain credentials")
        if approved and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise KnowledgeError("approved URL origin must not include path, query, or fragment")
        try:
            port = parsed.port
        except ValueError as exc:
            raise KnowledgeError("URL origin has an invalid port") from exc
        return (
            parsed.scheme.lower(),
            parsed.hostname.lower().rstrip("."),
            port if port is not None else (443 if parsed.scheme == "https" else 80),
        )

    @staticmethod
    def _sanitize_url(value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "unknown source"
        try:
            port = parsed.port
        except ValueError:
            return "unknown source"
        host = parsed.hostname.lower().rstrip(".")
        if ":" in host:
            host = f"[{host}]"
        default_port = 443 if parsed.scheme == "https" else 80
        authority = host if port in {None, default_port} else f"{host}:{port}"
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", "", ""))

    @classmethod
    def _sanitized_source_uri(cls, source: KnowledgeSource) -> str:
        if source.url:
            return cls._sanitize_url(source.url)
        return source.path or "unknown source"

    @classmethod
    def _path_matches(cls, path: str, prefix: str) -> bool:
        normalized = cls._normal_path(prefix)
        if normalized == "/":
            return True
        return path == normalized or path.startswith(normalized.rstrip("/") + "/")

    def _extract_bytes(self, content: bytes, suffix: str, content_type: str | None) -> str:
        if suffix not in _LOCAL_SUFFIXES:
            suffix = self._suffix_for_content_type(content_type)
        if suffix in {".txt", ".md", ".markdown"}:
            return content.decode("utf-8", errors="replace")
        if suffix == ".html":
            parser = _TextOnlyHTML()
            parser.feed(content.decode("utf-8", errors="replace"))
            return parser.text()
        if suffix == ".pdf":
            try:
                import pypdf
            except ImportError as exc:
                raise KnowledgeError("PDF extraction requires pypdf") from exc
            try:
                reader = pypdf.PdfReader(BytesIO(content))
                return "\n".join(page.extract_text() or "" for page in reader.pages)
            except Exception as exc:  # parser exceptions are untrusted-input failures
                raise KnowledgeError("PDF extraction failed") from exc
        if suffix == ".docx":
            try:
                from docx import Document
            except ImportError as exc:
                raise KnowledgeError("DOCX extraction requires python-docx") from exc
            try:
                return "\n".join(paragraph.text for paragraph in Document(BytesIO(content)).paragraphs)
            except Exception as exc:  # parser exceptions are untrusted-input failures
                raise KnowledgeError("DOCX extraction failed") from exc
        raise KnowledgeError("knowledge source file type is not allowed")

    @staticmethod
    def _suffix_for_content_type(content_type: str | None) -> str:
        return {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "text/plain": ".txt",
            "text/markdown": ".md",
            "text/html": ".html",
        }.get(content_type or "", "")

    def _normalize_text(self, value: str) -> str:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        normalized = "\n".join(" ".join(line.split()) for line in normalized.split("\n")).strip()
        if len(normalized) > self.max_chars:
            raise KnowledgeError("normalized text limit exceeded")
        return normalized

    @staticmethod
    def _facts(sources: list[_ExtractedSource]) -> tuple[list[KnowledgeFact], list[str]]:
        facts: list[KnowledgeFact] = []
        duplicates: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(sources, start=1):
            section = "document"
            for line in item.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if item.markdown and line.startswith("#"):
                    section = _anchor(line.lstrip("#").strip())
                    continue
                normalized = " ".join(line.split())
                key = normalized.casefold()
                if key in seen:
                    duplicates.append(normalized)
                    continue
                seen.add(key)
                facts.append(KnowledgeFact(normalized, item.uri, f"source-{index:03d}#{section}"))
        return facts, duplicates

    @staticmethod
    def _conflicts(facts: list[KnowledgeFact]) -> list[KnowledgeConflict]:
        by_subject: dict[str, list[KnowledgeFact]] = {}
        for fact in facts:
            if ":" not in fact.text:
                continue
            subject, value = fact.text.split(":", 1)
            if not subject.strip() or not value.strip():
                continue
            by_subject.setdefault(subject.casefold().strip(), []).append(fact)
        conflicts: list[KnowledgeConflict] = []
        for subject in sorted(by_subject):
            entries = by_subject[subject]
            values = tuple(dict.fromkeys(item.text.split(":", 1)[1].strip() for item in entries))
            if len(values) > 1:
                conflicts.append(
                    KnowledgeConflict(
                        subject=subject,
                        values=values,
                        source_locations=tuple(item.location for item in entries),
                    )
                )
        return conflicts

    @staticmethod
    def _missing_metadata(sources: list[_ExtractedSource]) -> tuple[str, ...]:
        missing: set[str] = set()
        for item in sources:
            if not item.source.owner.strip():
                missing.add("source owner is missing")
            if not item.source.effective_date.strip():
                missing.add("source effective date is missing")
        return tuple(sorted(missing))

    @staticmethod
    def _sensitive_findings(sources: list[_ExtractedSource]) -> tuple[str, ...]:
        text = "\n".join(item.text for item in sources)
        findings: list[str] = []
        if _EMAIL.search(text):
            findings.append("email address")
        if _PHONE.search(text):
            findings.append("phone number")
        return tuple(findings)

    @staticmethod
    def _write_review(sources: list[_ExtractedSource], review: KnowledgeReview, output_dir: Path) -> None:
        if output_dir.exists() and output_dir.is_symlink():
            raise KnowledgeError("knowledge output directory must not be a symlink")
        docs = output_dir / "knowledge_docs"
        if docs.exists() and docs.is_symlink():
            raise KnowledgeError("knowledge output directory must not be a symlink")
        if docs.exists() and not docs.is_dir():
            raise KnowledgeError("knowledge output directory collision")
        names = [f"source-{index:03d}.md" for index in range(1, len(sources) + 1)] + ["review.md"]
        expected = set(names)
        marker_prefix = f"<!-- smartpbx-agent-factory: knowledge-digest={review.digest} artifact="
        if docs.exists():
            for existing in docs.iterdir():
                if existing.name not in expected:
                    raise KnowledgeError("knowledge output directory contains an unexpected collision")
            for name in names:
                path = docs / name
                if not path.exists():
                    continue
                if path.is_symlink() or not path.is_file():
                    raise KnowledgeError("knowledge output file must not be a regular non-symlink file")
                with path.open("r", encoding="utf-8") as handle:
                    marker = handle.readline().rstrip("\n")
                if marker != f"{marker_prefix}{name} -->":
                    raise KnowledgeError("knowledge output file collision")
        docs.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(sources, start=1):
            name = f"source-{index:03d}.md"
            path = docs / name
            metadata = json.dumps(
                {
                    "source_uri": item.uri,
                    "owner": item.source.owner,
                    "effective_date": item.source.effective_date,
                    "classification": item.source.classification,
                    "section_anchors": _section_anchors(item.text) if item.markdown else ("document",),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            fence = "`" * (max((len(run) for run in re.findall(r"`+", item.text)), default=2) + 1)
            path.write_text(
                f"{marker_prefix}{name} -->\n# Imported knowledge source\n\n{metadata}\n\n"
                f"## Extracted text (untrusted data)\n\n{fence}\n{item.text}\n{fence}\n",
                encoding="utf-8",
            )
        report = {
            "digest": review.digest,
            "conflicts": [conflict.__dict__ for conflict in review.conflicts],
            "missing_facts": review.missing_facts,
            "sensitive_findings": review.sensitive_findings,
            "inaccessible_sources": review.inaccessible_sources,
            "duplicate_facts": review.duplicate_facts,
            "instruction_findings": review.instruction_findings,
        }
        (docs / "review.md").write_text(
            f"{marker_prefix}review.md -->\n# Knowledge review\n\n```json\n"
            + json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n```\n",
            encoding="utf-8",
        )


def _anchor(value: str) -> str:
    anchor = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return anchor or "document"


def _section_anchors(value: str) -> tuple[str, ...]:
    anchors = [_anchor(line.lstrip("#").strip()) for line in value.splitlines() if line.startswith("#")]
    return tuple(dict.fromkeys(anchors)) or ("document",)
