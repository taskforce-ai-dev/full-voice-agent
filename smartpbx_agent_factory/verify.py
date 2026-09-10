"""Fail-closed, privacy-safe generated SmartPBX verification contracts.

The verifier never starts Docker, binds a socket, or contacts a hostname.  A
CI-owned loopback adapter performs lifecycle work when supplied.  This module
only receives bounded booleans/counters from that adapter and never accepts a
caller-provided successful lifecycle result.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .provenance import TemplateAllowlist
from .resources import DerivedResources


class VerificationError(ValueError):
    """Raised when generated verification evidence is incomplete or unsafe."""


_PROTOCOL_FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "protocol_messages.json"
_PROVENANCE_FILE = ".smartpbx-factory-provenance.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
_CI_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$")
_REQUIRED_EVENTS = ("connected", "start", "media", "stop", "hangup")
_AUTH_CASES = ("missing", "wrong", "cross-agent", "valid")
_REQUIRED_FILES = (
    "Dockerfile",
    "server.py",
    "smartpbx_gateway.py",
    "smartpbx_protocol.py",
    "smartpbx_transport.py",
    "smartpbx_diagnostics.py",
    "docker-compose.yml",
    ".github-workflow-fragment.yml",
    _PROVENANCE_FILE,
)
_SAFE_DIAGNOSTIC_KEYS = {
    "active_sessions",
    "admitted_total",
    "released_total",
    "rejected_capacity_total",
    "frames_dropped_total",
    "protocol",
}
_SECRET_OR_CONTROL = re.compile(
    r"[\x00-\x1f\x7f]|(?:secret|password|credential|api[_-]?key|private[ _-]?key|token\s*[:=]|-----begin|\bsk-[A-Za-z0-9_-]{8,}|\bAKIA[0-9A-Z]{16}|\beyJ[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


def _safe_report_text(value: object, label: str, pattern: re.Pattern[str], *, limit: int = 120) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or not pattern.fullmatch(value):
        raise VerificationError(f"safe report {label} is required")
    if _SECRET_OR_CONTROL.search(value):
        raise VerificationError(f"safe report {label} is required")
    return value


def _resource_digest(resources: DerivedResources) -> str:
    values = {
        "slug": resources.slug,
        "python_identifier": resources.python_identifier,
        "website_service": resources.website_service,
        "smartpbx_service": resources.smartpbx_service,
        "website_port": resources.website_port,
        "smartpbx_port": resources.smartpbx_port,
        "website_hostname": resources.website_hostname,
        "smartpbx_hostname": resources.smartpbx_hostname,
        "wss_url": resources.wss_url,
        "wss_header": resources.wss_header,
        "status_url": resources.status_url,
        "ghcr_repository": resources.ghcr_repository,
        "ci_identifier": resources.ci_identifier,
        "folder_identity": resources.folder_identity,
        "secret_record_key": resources.secret_record_key,
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _allowlist_digest(allowlist: TemplateAllowlist) -> str:
    files = [
        {"source_path": source_path, "template_path": file.template_path, "sha256": file.sha256}
        for source_path, file in sorted(allowlist.files.items())
    ]
    value = {
        "template_version": allowlist.template_version,
        "source_revision": allowlist.source_revision,
        "image_digest": allowlist.image_digest,
        "oci_revision": allowlist.oci_revision,
        "protocol_version": allowlist.protocol_version,
        "environment_schema_version": allowlist.environment_schema_version,
        "files": files,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerificationBinding:
    """Trusted coordinator inputs that a generated tree cannot self-assert."""

    template_allowlist: TemplateAllowlist
    manifest_digest: str
    artifact_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.template_allowlist, TemplateAllowlist):
            raise VerificationError("approved template allowlist is required")
        _safe_report_text(self.template_allowlist.template_version, "template_version", _VERSION)
        if not _REVISION.fullmatch(self.template_allowlist.source_revision):
            raise VerificationError("approved template allowlist source_revision is invalid")
        if self.template_allowlist.oci_revision != self.template_allowlist.source_revision:
            raise VerificationError("approved template allowlist source revision is not OCI-bound")
        if not isinstance(self.template_allowlist.image_digest, str) or not _SHA256_REF.fullmatch(self.template_allowlist.image_digest):
            raise VerificationError("approved template allowlist image digest is invalid")
        if self.template_allowlist.protocol_version not in {"smartpbx-ai-provider-v06", "smartpbx-ai-provider-v07"}:
            raise VerificationError("approved template allowlist protocol is invalid")
        _safe_report_text(self.template_allowlist.environment_schema_version, "environment_schema_version", _VERSION)
        if not self.template_allowlist.files:
            raise VerificationError("approved template allowlist must map files")
        for source_path, entry in self.template_allowlist.files.items():
            if not isinstance(source_path, str) or not _SAFE_PATH.fullmatch(source_path) or "//" in source_path or "/../" in f"/{source_path}":
                raise VerificationError("approved template allowlist source path is invalid")
            if not hasattr(entry, "template_path") or not hasattr(entry, "sha256"):
                raise VerificationError("approved template allowlist entry is invalid")
            if not isinstance(entry.template_path, str) or not _SAFE_PATH.fullmatch(entry.template_path) or "//" in entry.template_path or "/../" in f"/{entry.template_path}":
                raise VerificationError("approved template allowlist target path is invalid")
            if not isinstance(entry.sha256, str) or not _SHA256_REF.fullmatch(entry.sha256):
                raise VerificationError("approved template allowlist file hash is invalid")
        if not isinstance(self.manifest_digest, str) or not _SHA256.fullmatch(self.manifest_digest):
            raise VerificationError("approved manifest digest is invalid")
        if not isinstance(self.artifact_digest, str) or not _SHA256.fullmatch(self.artifact_digest):
            raise VerificationError("approved artifact digest is invalid")

    def provenance_for(self, resources: DerivedResources) -> dict[str, str]:
        if not isinstance(resources, DerivedResources):
            raise VerificationError("verification requires derived resources")
        return {
            "agent_slug": resources.slug,
            "artifact_digest": self.artifact_digest,
            "manifest_digest": self.manifest_digest,
            "resource_digest": _resource_digest(resources),
            "source_revision": self.template_allowlist.source_revision,
            "template_allowlist_digest": _allowlist_digest(self.template_allowlist),
            "template_version": self.template_allowlist.template_version,
        }


@dataclass(frozen=True)
class DisposableClientResult:
    """The only non-sensitive result shape accepted from a lifecycle adapter."""

    connected: bool
    start_sent: bool
    start_accepted: bool
    media_sent: bool
    media_accepted: bool
    invalid_auth_rejected: bool
    terminal_event_observed: bool
    close_code: int
    active_tasks_after_close: int
    resources_after_close: int

    def __post_init__(self) -> None:
        if not all(isinstance(value, bool) for value in (self.connected, self.start_sent, self.start_accepted, self.media_sent, self.media_accepted, self.invalid_auth_rejected, self.terminal_event_observed)):
            raise VerificationError("disposable client evidence must contain booleans")
        for value in (self.active_tasks_after_close, self.resources_after_close):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000:
                raise VerificationError("disposable client counters must be bounded integers")
        if self.close_code not in {1000, 1008}:
            raise VerificationError("disposable client returned an unsupported close code")


class DisposableLifecycleAdapter(Protocol):
    """CI seam for a temporary loopback-only generated-backend lifecycle."""

    def exercise(
        self,
        *,
        agent_dir: Path,
        resources: DerivedResources,
        auth_case: Literal["missing", "wrong", "cross-agent", "valid"],
        messages: tuple[dict[str, object], ...],
    ) -> DisposableClientResult: ...


@dataclass(frozen=True)
class VerificationReport:
    agent_slug: str
    artifact_digest: str
    template_version: str
    source_revision: str
    ci_identifier: str
    protocol_events: tuple[str, ...]
    static_contracts_passed: bool
    runtime_lifecycle_verified: bool
    ready_for_pr: bool
    runtime_status: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        _safe_report_text(self.agent_slug, "agent_slug", _SLUG, limit=63)
        _safe_report_text(self.artifact_digest, "artifact_digest", _SHA256, limit=64)
        _safe_report_text(self.template_version, "template_version", _VERSION)
        _safe_report_text(self.source_revision, "source_revision", _REVISION, limit=40)
        _safe_report_text(self.ci_identifier, "ci_identifier", _CI_IDENTIFIER, limit=63)
        if self.protocol_events != _REQUIRED_EVENTS:
            raise VerificationError("safe report protocol events are invalid")
        if not all(isinstance(value, bool) for value in (self.static_contracts_passed, self.runtime_lifecycle_verified, self.ready_for_pr)):
            raise VerificationError("safe report booleans are required")
        if self.runtime_status not in {"CI_LIFECYCLE_REQUIRED", "CI_LIFECYCLE_VERIFIED"}:
            raise VerificationError("safe report runtime status is invalid")
        if self.ready_for_pr != self.runtime_lifecycle_verified:
            raise VerificationError("safe report readiness must match lifecycle evidence")
        if not self.static_contracts_passed or not self.evidence or not all(isinstance(item, str) and item in _REQUIRED_FILES for item in self.evidence):
            raise VerificationError("safe report evidence is invalid")


def _safe_protocol_value(value: object, *, depth: int = 0) -> None:
    if depth > 2:
        raise VerificationError("protocol fixture nesting is unsafe")
    if isinstance(value, dict):
        if not all(isinstance(key, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) for key in value):
            raise VerificationError("protocol fixture keys are unsafe")
        for item in value.values():
            _safe_protocol_value(item, depth=depth + 1)
        return
    if not isinstance(value, str) or len(value) > 80 or _SECRET_OR_CONTROL.search(value):
        raise VerificationError("protocol fixture values are unsafe")


def _read_protocol_messages(path: Path = _PROTOCOL_FIXTURE) -> tuple[dict[str, object], ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("protocol fixture is unavailable") from exc
    if not isinstance(raw, list) or len(raw) != len(_REQUIRED_EVENTS):
        raise VerificationError("protocol fixture must be a five-event list")
    messages: list[dict[str, object]] = []
    for event, item in zip(_REQUIRED_EVENTS, raw):
        if not isinstance(item, dict) or set(item) != {"event", event} or item.get("event") != event or not isinstance(item[event], dict):
            raise VerificationError("protocol fixture has an invalid production-shaped message")
        _safe_protocol_value(item[event])
        messages.append({"event": event, event: dict(item[event])})
    media = messages[2]["media"]
    if media != {"track": "inbound", "payload": "<synthetic-silence>"}:
        raise VerificationError("protocol fixture media must be synthetic silence only")
    return tuple(messages)


def _validate_lifecycle_result(result: object, auth_case: str) -> DisposableClientResult:
    if not isinstance(result, DisposableClientResult):
        raise VerificationError("lifecycle adapter returned invalid bounded evidence")
    if auth_case == "valid":
        if not (result.connected and result.start_sent and result.start_accepted and result.media_sent and result.media_accepted):
            raise VerificationError("disposable lifecycle did not accept connected/start/media")
        if result.invalid_auth_rejected or not result.terminal_event_observed or result.close_code != 1000:
            raise VerificationError("disposable lifecycle did not observe clean termination")
    elif not result.invalid_auth_rejected or result.close_code != 1008 or result.connected or result.start_sent or result.start_accepted or result.media_sent or result.media_accepted:
        raise VerificationError("invalid authentication must close before connected/start/media")
    if result.active_tasks_after_close or result.resources_after_close:
        raise VerificationError("disposable lifecycle leaked tasks or resources")
    return result


def run_disposable_client(
    adapter: DisposableLifecycleAdapter,
    *,
    agent_dir: Path,
    resources: DerivedResources,
    auth_case: Literal["missing", "wrong", "cross-agent", "valid"],
    protocol_fixture: Path = _PROTOCOL_FIXTURE,
) -> DisposableClientResult:
    """Invoke the CI adapter with privacy-safe protocol shapes and validate it."""
    if auth_case not in _AUTH_CASES:
        raise VerificationError("unknown disposable authentication case")
    try:
        result = adapter.exercise(agent_dir=Path(agent_dir), resources=resources, auth_case=auth_case, messages=_read_protocol_messages(protocol_fixture))
    except VerificationError:
        raise
    except Exception as exc:
        raise VerificationError(f"lifecycle adapter failed for {auth_case}") from exc
    return _validate_lifecycle_result(result, auth_case)


def _artifact_digest(agent_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(agent_dir.rglob("*")):
        if path.is_symlink():
            raise VerificationError("generated backend may not contain symlinks")
        if path.is_file() and path.name != _PROVENANCE_FILE:
            digest.update(path.relative_to(agent_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _read_required(agent_dir: Path, relative: str) -> str:
    path = agent_dir / relative
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"missing generated contract file: {relative}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"generated contract file is not UTF-8: {relative}") from exc


def _provenance(agent_dir: Path, binding: VerificationBinding, resources: DerivedResources) -> tuple[str, str, str]:
    actual_artifact = _artifact_digest(agent_dir)
    if actual_artifact != binding.artifact_digest:
        raise VerificationError("generated backend does not match approved artifact digest")
    try:
        raw = json.loads(_read_required(agent_dir, _PROVENANCE_FILE))
    except json.JSONDecodeError as exc:
        raise VerificationError("verification provenance is invalid JSON") from exc
    if raw != binding.provenance_for(resources):
        raise VerificationError("verification provenance does not match approved binding")
    return binding.template_allowlist.template_version, binding.template_allowlist.source_revision, actual_artifact


def _diagnostics_contract(source: str) -> None:
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise VerificationError("generated diagnostics are not valid Python") from exc
    functions = [node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "redacted_status"]
    if len(functions) != 1:
        raise VerificationError("generated diagnostics must define one redacted_status contract")
    returns = [node.value for node in ast.walk(functions[0]) if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)]
    if len(returns) != 1:
        raise VerificationError("generated diagnostics must return one bounded mapping")
    keys = returns[0].keys
    if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) or key.value not in _SAFE_DIAGNOSTIC_KEYS for key in keys):
        raise VerificationError("generated diagnostics return unsafe evidence")


def _require_static_contracts(agent_dir: Path, resources: DerivedResources) -> tuple[str, ...]:
    if not isinstance(resources, DerivedResources):
        raise VerificationError("verification requires derived resources")
    if not agent_dir.is_dir() or agent_dir.is_symlink():
        raise VerificationError("generated backend must be a real directory")
    contents = {relative: _read_required(agent_dir, relative) for relative in _REQUIRED_FILES}
    if "COPY . ." not in contents["Dockerfile"]:
        raise VerificationError("generated Dockerfile must copy the generated backend")
    for route in ("/health", "/smartpbx/status", "/ws/v1/smartpbx/media"):
        if route not in contents["server.py"]:
            raise VerificationError(f"generated server is missing required route: {route}")
    if "STATUS_AUTHENTICATED" not in contents["server.py"] or "SMARTPBX_STATUS_TOKEN" not in contents["docker-compose.yml"]:
        raise VerificationError("generated status endpoint must be authenticated")
    if resources.wss_header not in contents["docker-compose.yml"]:
        raise VerificationError("generated compose is missing the agent-specific WSS header")
    gateway = contents["smartpbx_gateway.py"]
    compare_at, accept_at = gateway.find("secrets.compare_digest"), gateway.find("await websocket.accept")
    if compare_at < 0 or accept_at < 0 or compare_at > accept_at:
        raise VerificationError("generated WSS authentication must be constant-time before accept")
    if "1008" not in gateway:
        raise VerificationError("generated WSS authentication must reject invalid credentials")
    if resources.ci_identifier not in contents[".github-workflow-fragment.yml"]:
        raise VerificationError("generated backend is missing agent-specific blocking CI")
    _diagnostics_contract(contents["smartpbx_diagnostics.py"])
    return tuple(sorted(_REQUIRED_FILES))


def verify_generated_backend(
    agent_dir: Path,
    resources: DerivedResources,
    *,
    binding: VerificationBinding,
    lifecycle_adapter: DisposableLifecycleAdapter | None = None,
) -> VerificationReport:
    """Verify static contracts and invoke a CI lifecycle adapter when provided.

    ``binding`` must come from the approved manifest/template/render transaction.
    It is deliberately required: a generated artifact cannot self-authorize by
    rewriting its sidecar metadata.  No adapter is a valid static result but is
    never PR-ready.
    """
    if not isinstance(binding, VerificationBinding):
        raise VerificationError("approved verification binding is required")
    agent_dir = Path(agent_dir)
    contracts = _require_static_contracts(agent_dir, resources)
    template_version, source_revision, artifact_digest = _provenance(agent_dir, binding, resources)
    runtime_verified = False
    if lifecycle_adapter is not None:
        for auth_case in _AUTH_CASES:
            run_disposable_client(lifecycle_adapter, agent_dir=agent_dir, resources=resources, auth_case=auth_case)
        runtime_verified = True
    return VerificationReport(
        agent_slug=resources.slug,
        artifact_digest=artifact_digest,
        template_version=template_version,
        source_revision=source_revision,
        ci_identifier=resources.ci_identifier,
        protocol_events=_REQUIRED_EVENTS,
        static_contracts_passed=True,
        runtime_lifecycle_verified=runtime_verified,
        ready_for_pr=runtime_verified,
        runtime_status="CI_LIFECYCLE_VERIFIED" if runtime_verified else "CI_LIFECYCLE_REQUIRED",
        evidence=contracts,
    )


def _reports(report_set: Mapping[str, VerificationReport] | Sequence[VerificationReport]) -> tuple[VerificationReport, ...]:
    values = tuple(report_set.values()) if isinstance(report_set, Mapping) else tuple(report_set)
    if not values or not all(isinstance(report, VerificationReport) for report in values):
        raise VerificationError("readiness report requires one or more verification reports")
    return values


def readiness_report(
    report_set: Mapping[str, VerificationReport] | Sequence[VerificationReport], *, secret_values: Sequence[str] = ()
) -> str:
    """Render validated, redacted coordinator metadata without payload evidence."""
    reports = _reports(report_set)
    ready = all(report.ready_for_pr for report in reports)
    lines = [f"readiness: {'VERIFIED' if ready else 'BLOCKED'}"]
    for report in sorted(reports, key=lambda item: item.agent_slug):
        lines.extend(
            (
                f"agent_slug: {report.agent_slug}",
                f"artifact_digest: {report.artifact_digest}",
                f"template_version: {report.template_version}",
                f"source_revision: {report.source_revision}",
                f"ci_identifier: {report.ci_identifier}",
                f"runtime_status: {report.runtime_status}",
                f"static_contracts_passed: {str(report.static_contracts_passed).lower()}",
                f"runtime_lifecycle_verified: {str(report.runtime_lifecycle_verified).lower()}",
            )
        )
    rendered = "\n".join(lines) + "\n"
    for value in secret_values:
        if isinstance(value, str) and value:
            rendered = rendered.replace(value, "[REDACTED]")
    if _SECRET_OR_CONTROL.search(rendered):
        raise VerificationError("safe report rendering failed")
    return rendered
