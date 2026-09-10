"""Fail-closed, privacy-safe contracts for generated SmartPBX verification.

This module deliberately does not start Docker, bind sockets, or contact a
public host.  Runtime lifecycle exercise is an injected CI-only seam; without
that evidence a report remains blocked.  The structural checks are useful both
before CI and when deciding whether an artifact is eligible for that exercise.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .resources import DerivedResources


class VerificationError(ValueError):
    """Raised when a generated backend lacks a required safety contract."""


@dataclass(frozen=True)
class DisposableClientResult:
    """Bounded, non-sensitive evidence returned by a disposable lifecycle run."""

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
        for value in (self.active_tasks_after_close, self.resources_after_close):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1_000:
                raise VerificationError("disposable client counters must be bounded integers")
        if self.close_code not in {1000, 1008}:
            raise VerificationError("disposable client returned an unsupported close code")


class DisposableBackend(Protocol):
    """CI adapter seam for a loopback-only, generation-scoped backend image."""

    def exercise(
        self, *, header: str, messages: tuple[dict[str, object], ...]
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


_PROTOCOL_FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "protocol_messages.json"
_PROVENANCE_FILE = ".smartpbx-factory-provenance.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_EVENTS = ("connected", "start", "media", "stop", "hangup")
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
_FORBIDDEN_DIAGNOSTIC_TERMS = ("transcript", "audio_bytes", "caller_number", "provider_response")


def _read_protocol_messages(path: Path = _PROTOCOL_FIXTURE) -> tuple[dict[str, object], ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("protocol fixture is unavailable") from exc
    if not isinstance(raw, list):
        raise VerificationError("protocol fixture must be a list")
    messages: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"event", "fields"}:
            raise VerificationError("protocol fixture has an invalid message shape")
        event, fields = item["event"], item["fields"]
        if not isinstance(event, str) or not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
            raise VerificationError("protocol fixture has invalid event fields")
        messages.append({"event": event, "fields": list(fields)})
    if tuple(item["event"] for item in messages) != _REQUIRED_EVENTS:
        raise VerificationError("protocol fixture must define connected/start/media/stop/hangup")
    return tuple(messages)


def run_disposable_client(
    backend: DisposableBackend, *, header: str, protocol_fixture: Path = _PROTOCOL_FIXTURE
) -> DisposableClientResult:
    """Exercise an injected loopback adapter without retaining media or call data.

    ``header`` is passed directly to the adapter and is intentionally excluded
    from the result and from every report.  The fixture contains field names,
    not media payloads or any customer identifiers.
    """
    if not isinstance(header, str) or not header:
        raise VerificationError("disposable client requires a non-empty header value")
    result = backend.exercise(header=header, messages=_read_protocol_messages(protocol_fixture))
    if not isinstance(result, DisposableClientResult):
        raise VerificationError("disposable backend returned invalid lifecycle evidence")
    if result.invalid_auth_rejected:
        if result.close_code != 1008 or result.start_sent or result.start_accepted:
            raise VerificationError("invalid authentication must close before start")
        return result
    if not (result.connected and result.start_sent and result.start_accepted and result.media_sent and result.media_accepted):
        raise VerificationError("disposable lifecycle did not accept connected/start/media")
    if not result.terminal_event_observed or result.close_code != 1000:
        raise VerificationError("disposable lifecycle did not observe clean termination")
    if result.active_tasks_after_close or result.resources_after_close:
        raise VerificationError("disposable lifecycle leaked tasks or resources")
    return result


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


def _provenance(agent_dir: Path) -> tuple[str, str, str]:
    try:
        raw = json.loads(_read_required(agent_dir, _PROVENANCE_FILE))
    except json.JSONDecodeError as exc:
        raise VerificationError("verification provenance is invalid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"template_version", "source_revision", "artifact_digest"}:
        raise VerificationError("verification provenance must contain only template_version, source_revision, artifact_digest")
    template_version = raw["template_version"]
    source_revision = raw["source_revision"]
    artifact_digest = raw["artifact_digest"]
    if not isinstance(template_version, str) or not template_version:
        raise VerificationError("verification provenance has no template_version")
    if not isinstance(source_revision, str) or not _REVISION.fullmatch(source_revision):
        raise VerificationError("verification provenance has invalid source_revision")
    if not isinstance(artifact_digest, str) or not _SHA256.fullmatch(artifact_digest):
        raise VerificationError("verification provenance has invalid artifact_digest")
    if artifact_digest != _artifact_digest(agent_dir):
        raise VerificationError("verification provenance artifact digest does not match generated backend")
    return template_version, source_revision, artifact_digest


def _require_static_contracts(agent_dir: Path, resources: DerivedResources) -> tuple[str, ...]:
    if not isinstance(resources, DerivedResources):
        raise VerificationError("verification requires derived resources")
    if not agent_dir.is_dir() or agent_dir.is_symlink():
        raise VerificationError("generated backend must be a real directory")
    contents = {relative: _read_required(agent_dir, relative) for relative in _REQUIRED_FILES}
    if "COPY . ." not in contents["Dockerfile"]:
        raise VerificationError("generated Dockerfile must copy the generated backend")
    server = contents["server.py"]
    for route in ("/health", "/smartpbx/status", "/ws/v1/smartpbx/media"):
        if route not in server:
            raise VerificationError(f"generated server is missing required route: {route}")
    if "STATUS_AUTHENTICATED" not in server or "SMARTPBX_STATUS_TOKEN" not in contents["docker-compose.yml"]:
        raise VerificationError("generated status endpoint must be authenticated")
    compose = contents["docker-compose.yml"]
    if resources.wss_header not in compose:
        raise VerificationError("generated compose is missing the agent-specific WSS header")
    gateway = contents["smartpbx_gateway.py"]
    compare_at = gateway.find("secrets.compare_digest")
    accept_at = gateway.find("await websocket.accept")
    if compare_at < 0 or accept_at < 0 or compare_at > accept_at:
        raise VerificationError("generated WSS authentication must be constant-time before accept")
    if "1008" not in gateway:
        raise VerificationError("generated WSS authentication must reject invalid credentials")
    if resources.ci_identifier not in contents[".github-workflow-fragment.yml"]:
        raise VerificationError("generated backend is missing agent-specific blocking CI")
    diagnostics = contents["smartpbx_diagnostics.py"].lower()
    if any(term in diagnostics for term in _FORBIDDEN_DIAGNOSTIC_TERMS):
        raise VerificationError("generated diagnostics are not privacy-safe")
    return tuple(sorted(_REQUIRED_FILES))


def verify_generated_backend(
    agent_dir: Path, resources: DerivedResources, *, lifecycle: DisposableClientResult | None = None
) -> VerificationReport:
    """Verify immutable artifact contracts; runtime evidence is CI-only and optional.

    No lifecycle result means the report is explicitly not ready for a PR.  A
    future orchestrator may supply the bounded result returned by a CI adapter;
    this module owns neither Docker execution nor stage mutation.
    """
    agent_dir = Path(agent_dir)
    contracts = _require_static_contracts(agent_dir, resources)
    template_version, source_revision, artifact_digest = _provenance(agent_dir)
    runtime_verified = False
    if lifecycle is not None:
        if not isinstance(lifecycle, DisposableClientResult):
            raise VerificationError("lifecycle evidence must be a DisposableClientResult")
        if lifecycle.invalid_auth_rejected:
            raise VerificationError("accepted lifecycle evidence cannot be an invalid-auth attempt")
        if not (lifecycle.connected and lifecycle.start_sent and lifecycle.start_accepted and lifecycle.media_sent and lifecycle.media_accepted):
            raise VerificationError("lifecycle evidence is incomplete")
        if not lifecycle.terminal_event_observed or lifecycle.close_code != 1000:
            raise VerificationError("lifecycle evidence lacks clean termination")
        if lifecycle.active_tasks_after_close or lifecycle.resources_after_close:
            raise VerificationError("lifecycle evidence shows leaked resources")
        runtime_verified = True
    return VerificationReport(
        agent_slug=resources.slug,
        artifact_digest=artifact_digest,
        template_version=template_version,
        source_revision=source_revision,
        ci_identifier=resources.ci_identifier,
        protocol_events=tuple(item["event"] for item in _read_protocol_messages()),
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
    """Render redacted readiness metadata for the coordinator integration seam."""
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
    if any(term in rendered.lower() for term in _FORBIDDEN_DIAGNOSTIC_TERMS):
        raise VerificationError("readiness report would contain prohibited diagnostics")
    return rendered
