"""Bounded, review-only ingestion for factory knowledge sources.

Source material is untrusted data.  This module extracts text for a human
review; it never interprets source text as instructions or executes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import posixpath
import re
from typing import Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model import KnowledgeSource
from .state import GenerationState


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TOTAL_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_CHARS = 200_000
DEFAULT_MAX_REDIRECTS = 8
DEFAULT_TIMEOUT_SECONDS = 10.0

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
class KnowledgeReview:
    facts: tuple[KnowledgeFact, ...]
    conflicts: tuple[KnowledgeConflict, ...]
    missing_facts: tuple[str, ...]
    sensitive_findings: tuple[str, ...]
    inaccessible_sources: tuple[str, ...]
    duplicate_facts: tuple[str, ...]
    instruction_findings: tuple[str, ...]
    digest: str
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


class KnowledgeBuilder(Protocol):
    def build(self, sources: tuple[KnowledgeSource, ...], output_dir: Path) -> KnowledgeReview:
        """Extract bounded source data and return the human-review artifact."""


@dataclass(frozen=True)
class _ExtractedSource:
    source: KnowledgeSource
    uri: str
    text: str
    byte_count: int


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


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, check_url, max_redirects: int) -> None:
        super().__init__()
        self._check_url = check_url
        self._max_redirects = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urljoin(req.full_url, newurl)
        redirects = int(getattr(req, "_knowledge_redirects", 0)) + 1
        if redirects > self._max_redirects:
            raise KnowledgeError("URL redirect limit exceeded")
        self._check_url(target)
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is not None:
            setattr(redirected, "_knowledge_redirects", redirects)
        return redirected


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
    ) -> None:
        if min(max_bytes, total_max_bytes, max_chars, max_redirects) <= 0 or timeout_seconds <= 0:
            raise KnowledgeError("knowledge limits must be positive")
        self.max_bytes = max_bytes
        self.total_max_bytes = total_max_bytes
        self.max_chars = max_chars
        self.max_redirects = max_redirects
        self.timeout_seconds = timeout_seconds
        self.approved_source_roots = tuple(Path(root) for root in approved_source_roots or ())

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
                inaccessible.append(source.url or source.path or "unknown source")
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
        digest = self._digest(extracted, facts, conflicts, missing, sensitive, inaccessible, duplicates, instructions)
        review = KnowledgeReview(
            facts=tuple(facts),
            conflicts=tuple(conflicts),
            missing_facts=missing,
            sensitive_findings=sensitive,
            inaccessible_sources=tuple(inaccessible),
            duplicate_facts=tuple(duplicates),
            instruction_findings=instructions,
            digest=digest,
        )
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
            return _ExtractedSource(source, path.as_uri(), text, len(content))
        if source.kind == "url":
            if not source.url or source.path:
                raise KnowledgeError("URL knowledge source requires url only")
            content, content_type, effective_url = self._fetch_url(source)
            suffix = Path(urlsplit(effective_url).path).suffix.lower()
            text = self._normalize_text(self._extract_bytes(content, suffix, content_type))
            return _ExtractedSource(source, effective_url, text, len(content))
        raise KnowledgeError("knowledge source kind must be local or url")

    def _safe_local_path(self, value: str) -> Path:
        raw = Path(value).expanduser()
        if "\x00" in value or any(part == ".." for part in raw.parts):
            raise KnowledgeError("local knowledge source is outside approved root")
        candidates: tuple[tuple[Path, Path], ...]
        if raw.is_absolute():
            roots = self.approved_source_roots
            if not roots:
                roots = (raw.parent,)
            candidates = tuple((Path(root), raw) for root in roots)
        else:
            roots = self.approved_source_roots
            if not roots:
                raise KnowledgeError("relative local knowledge source requires approved source root")
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
        def check_url(value: str) -> None:
            self._check_allowed_url(value, source)

        check_url(source.url or "")
        opener = build_opener(_AllowlistedRedirectHandler(check_url, self.max_redirects))
        request = Request(source.url, headers={"Accept": ", ".join(sorted(_ALLOWED_CONTENT_TYPES))})
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                content_type = response.headers.get_content_type().lower()
                if content_type not in _ALLOWED_CONTENT_TYPES:
                    raise KnowledgeError("URL content type is not allowed")
                declared = response.headers.get("Content-Length")
                if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
                    raise KnowledgeError("source byte limit exceeded")
                content = response.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise KnowledgeError("source byte limit exceeded")
                return content, content_type, response.geturl()
        except KnowledgeError:
            raise
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise _SourceUnavailable from exc

    @staticmethod
    def _normal_path(value: str) -> str:
        path = unquote(value or "/")
        return "/" + posixpath.normpath("/" + path).lstrip("/")

    def _check_allowed_url(self, value: str, source: KnowledgeSource) -> None:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise KnowledgeError("URL must have an HTTP(S) origin")
        if parsed.username or parsed.password:
            raise KnowledgeError("URL credentials are not allowed")
        host = parsed.hostname.lower().rstrip(".")
        allowed_hosts = {self._origin_host(origin) for origin in source.approved_origins}
        if host not in allowed_hosts:
            raise KnowledgeError("URL origin is not allowlisted")
        path = self._normal_path(parsed.path)
        if source.path_prefixes and not any(self._path_matches(path, prefix) for prefix in source.path_prefixes):
            raise KnowledgeError("URL path is outside the allowlisted prefix")

    @staticmethod
    def _origin_host(origin: str) -> str:
        parsed = urlsplit(origin if "://" in origin else f"//{origin}")
        if not parsed.hostname:
            raise KnowledgeError("approved URL origin is invalid")
        return parsed.hostname.lower().rstrip(".")

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
            first_content = True
            for line in item.text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    section = _anchor(line.lstrip("#").strip())
                    first_content = False
                    continue
                if first_content:
                    section = _anchor(line)
                    first_content = False
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
    def _digest(
        sources: list[_ExtractedSource],
        facts: list[KnowledgeFact],
        conflicts: list[KnowledgeConflict],
        missing: tuple[str, ...],
        sensitive: tuple[str, ...],
        inaccessible: list[str],
        duplicates: list[str],
        instructions: tuple[str, ...],
    ) -> str:
        payload = {
            "sources": [
                {
                    "uri": item.uri,
                    "owner": item.source.owner,
                    "effective_date": item.source.effective_date,
                    "classification": item.source.classification,
                    "text": item.text,
                }
                for item in sources
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

    @staticmethod
    def _write_review(sources: list[_ExtractedSource], review: KnowledgeReview, output_dir: Path) -> None:
        if output_dir.exists() and output_dir.is_symlink():
            raise KnowledgeError("knowledge output directory must not be a symlink")
        docs = output_dir / "knowledge_docs"
        if docs.exists() and docs.is_symlink():
            raise KnowledgeError("knowledge output directory must not be a symlink")
        docs.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(sources, start=1):
            path = docs / f"source-{index:03d}.md"
            if path.exists() and path.is_symlink():
                raise KnowledgeError("knowledge output file must not be a symlink")
            metadata = json.dumps(
                {
                    "source_uri": item.uri,
                    "owner": item.source.owner,
                    "effective_date": item.source.effective_date,
                    "classification": item.source.classification,
                    "section_anchors": _section_anchors(item.text),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            fence = "`" * (max((len(run) for run in re.findall(r"`+", item.text)), default=2) + 1)
            path.write_text(
                f"# Imported knowledge source\n\n{metadata}\n\n"
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
            "# Knowledge review\n\n```json\n"
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
