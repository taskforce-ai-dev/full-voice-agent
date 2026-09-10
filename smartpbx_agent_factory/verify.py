"""Fail-closed, privacy-safe generated SmartPBX verification contracts.

The verifier never starts Docker, binds a socket, or contacts a hostname.  The
repository-owned CI lifecycle runner performs those observations directly;
this module has no caller-injectable lifecycle-success seam.
"""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

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
_CI_CHECK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,119}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_RUN_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$")
_REQUIRED_EVENTS = ("connected", "start", "media", "stop", "hangup")
_STATIC_CI_ROLES = ("operations", "website")
_TERMINAL_PATHS = ("stop", "hangup")
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
    r"[\x00-\x09\x0b-\x1f\x7f]|(?:secret|password|credential|api[_-]?key|private[ _-]?key|token\s*[:=]|-----begin|\bsk-[A-Za-z0-9_-]{8,}|\bAKIA[0-9A-Z]{16}|\beyJ[A-Za-z0-9_-]{8,})",
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


def template_allowlist_digest(allowlist: TemplateAllowlist) -> str:
    """Return the stable digest used by generated provenance and CI evidence."""
    return _allowlist_digest(allowlist)


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
    role: str = "backend"
    ci_policy: str = "lifecycle-attestation"
    repository: str = ""
    ci_check: str = ""
    head_sha: str = ""

    def __post_init__(self) -> None:
        _safe_report_text(self.agent_slug, "agent_slug", _SLUG, limit=63)
        _safe_report_text(self.artifact_digest, "artifact_digest", _SHA256, limit=64)
        _safe_report_text(self.template_version, "template_version", _VERSION)
        _safe_report_text(self.source_revision, "source_revision", _REVISION, limit=40)
        _safe_report_text(self.ci_identifier, "ci_identifier", _CI_IDENTIFIER, limit=63)
        if self.role not in {"backend", *_STATIC_CI_ROLES}:
            raise VerificationError("safe report role is invalid")
        expected_policy = "lifecycle-attestation" if self.role == "backend" else (
            "secret-static" if self.role == "operations" else "website-build"
        )
        if self.ci_policy != expected_policy:
            raise VerificationError("safe report CI policy is invalid")
        if self.repository:
            _safe_report_text(self.repository, "repository", _REPOSITORY, limit=201)
        if self.ci_check:
            _safe_report_text(self.ci_check, "ci_check", _CI_CHECK)
        if self.head_sha:
            _safe_report_text(self.head_sha, "head_sha", _REVISION, limit=40)
        if not all(isinstance(value, bool) for value in (self.static_contracts_passed, self.runtime_lifecycle_verified, self.ready_for_pr)):
            raise VerificationError("safe report booleans are required")
        if self.role == "backend":
            if self.protocol_events != _REQUIRED_EVENTS:
                raise VerificationError("safe report protocol events are invalid")
            if self.runtime_status not in {"CI_LIFECYCLE_REQUIRED", "CI_LIFECYCLE_VERIFIED"}:
                raise VerificationError("safe report runtime status is invalid")
            if self.ready_for_pr != self.runtime_lifecycle_verified:
                raise VerificationError("safe report readiness must match lifecycle evidence")
            if not self.static_contracts_passed or not self.evidence or not all(isinstance(item, str) and item in _REQUIRED_FILES for item in self.evidence):
                raise VerificationError("safe report evidence is invalid")
            return
        if (
            self.protocol_events
            or self.runtime_lifecycle_verified
            or not self.ready_for_pr
            or self.runtime_status != "CI_STATIC_VERIFIED"
            or self.evidence != ("github-ci-check",)
            or not self.repository
            or not self.ci_check
            or not self.head_sha
        ):
            raise VerificationError("static CI report must not claim runtime lifecycle evidence")


_ATTESTATION_CASES = (
    "status-auth-rejected",
    "wss-auth-rejected",
    "cross-agent-rejected",
    "stop-cleanup",
    "hangup-cleanup",
)


@dataclass(frozen=True)
class LifecycleAttestation:
    """Repository-owned, redacted CI observation bound to one exact artifact."""

    repository: str
    lane: str
    head_sha: str
    run_id: str
    artifact_digest: str
    source_revision: str
    template_version: str
    template_allowlist_digest: str
    candidate_provenance_digest: str
    fixture_kind: str
    observed_cases: tuple[str, ...]

    def __post_init__(self) -> None:
        _safe_report_text(self.repository, "attestation repository", _REPOSITORY, limit=201)
        _safe_report_text(self.lane, "attestation lane", _CI_IDENTIFIER, limit=63)
        _safe_report_text(self.head_sha, "attestation head_sha", _REVISION, limit=40)
        _safe_report_text(self.run_id, "attestation run_id", _RUN_ID, limit=20)
        _safe_report_text(self.artifact_digest, "attestation artifact_digest", _SHA256, limit=64)
        _safe_report_text(self.source_revision, "attestation source_revision", _REVISION, limit=40)
        _safe_report_text(self.template_version, "attestation template_version", _VERSION)
        if self.lane != "backend" or self.fixture_kind not in {"generated-agent", "canonical-review-only"} or self.observed_cases != _ATTESTATION_CASES:
            raise VerificationError("lifecycle attestation has an invalid fixed contract")
        if self.fixture_kind == "generated-agent":
            _safe_report_text(self.template_allowlist_digest, "attestation template_allowlist_digest", _SHA256, limit=64)
            if self.candidate_provenance_digest:
                raise VerificationError("generated lifecycle attestation has unexpected candidate evidence")
        else:
            _safe_report_text(self.candidate_provenance_digest, "attestation candidate_provenance_digest", _SHA256, limit=64)
            if self.template_allowlist_digest and not _SHA256.fullmatch(self.template_allowlist_digest):
                raise VerificationError("canonical lifecycle attestation has invalid template evidence")


def load_lifecycle_attestation(
    path: Path, *, agent_dir: Path, repository: str, lane: str, head_sha: str, run_id: str,
) -> LifecycleAttestation:
    """Load only a repository-produced lifecycle observation, never caller booleans."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("lifecycle attestation is unavailable") from exc
    expected_keys = {
        "schema_version", "repository", "lane", "head_sha", "run_id", "artifact_digest", "source_revision", "template_version",
        "template_allowlist_digest", "candidate_provenance_digest", "fixture_kind", "observed_cases",
    }
    if (
        not isinstance(raw, dict) or set(raw) != expected_keys or raw.get("schema_version") != 1
        or raw.get("repository") != repository or raw.get("lane") != lane
        or raw.get("head_sha") != head_sha or raw.get("run_id") != run_id
    ):
        raise VerificationError("lifecycle attestation has an invalid repository schema")
    observed = raw.get("observed_cases")
    if not isinstance(observed, list) or not all(isinstance(item, str) for item in observed):
        raise VerificationError("lifecycle attestation observed cases are invalid")
    attestation = LifecycleAttestation(
        repository=raw["repository"], lane=raw["lane"], head_sha=raw["head_sha"], run_id=raw["run_id"], artifact_digest=raw["artifact_digest"],
        source_revision=raw["source_revision"], template_version=raw["template_version"],
        template_allowlist_digest=raw["template_allowlist_digest"],
        candidate_provenance_digest=raw["candidate_provenance_digest"], fixture_kind=raw["fixture_kind"],
        observed_cases=tuple(observed),
    )
    agent_dir = Path(agent_dir)
    if attestation.artifact_digest != _artifact_digest(agent_dir):
        raise VerificationError("lifecycle attestation does not match the exact generated artifact")
    try:
        provenance = json.loads(_read_required(agent_dir, _PROVENANCE_FILE))
    except json.JSONDecodeError as exc:
        raise VerificationError("lifecycle attestation generated provenance is invalid") from exc
    if not isinstance(provenance, dict) or any(
        provenance.get(key) != value for key, value in (
            ("artifact_digest", attestation.artifact_digest),
            ("source_revision", attestation.source_revision),
            ("template_version", attestation.template_version),
        )
    ):
        raise VerificationError("lifecycle attestation provenance binding changed")
    if attestation.fixture_kind == "generated-agent":
        if provenance.get("template_allowlist_digest") != attestation.template_allowlist_digest:
            raise VerificationError("lifecycle attestation allowlist binding changed")
    elif provenance.get("candidate_provenance_digest") != attestation.candidate_provenance_digest:
        raise VerificationError("lifecycle attestation candidate binding changed")
    return attestation


def _safe_protocol_value(value: object, *, depth: int = 0) -> None:
    if depth > 2:
        raise VerificationError("protocol fixture nesting is unsafe")
    if isinstance(value, dict):
        if not all(isinstance(key, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key) for key in value):
            raise VerificationError("protocol fixture keys are unsafe")
        for item in value.values():
            _safe_protocol_value(item, depth=depth + 1)
        return
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
        return
    if not isinstance(value, str) or len(value) > 80 or _SECRET_OR_CONTROL.search(value):
        raise VerificationError("protocol fixture values are unsafe")


def _read_protocol_scenarios(path: Path = _PROTOCOL_FIXTURE) -> Mapping[str, tuple[dict[str, object], ...]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("protocol fixture is unavailable") from exc
    if not isinstance(raw, dict) or set(raw) != set(_TERMINAL_PATHS):
        raise VerificationError("protocol fixture must define stop and hangup scenarios")
    scenarios: dict[str, tuple[dict[str, object], ...]] = {}
    for terminal_path in _TERMINAL_PATHS:
        items = raw[terminal_path]
        expected_events = ("connected", "start", "media", terminal_path)
        if not isinstance(items, list) or len(items) != len(expected_events):
            raise VerificationError("protocol fixture terminal scenario has an invalid length")
        messages: list[dict[str, object]] = []
        for event, item in zip(expected_events, items):
            if not isinstance(item, dict) or set(item) != {"event", event} or item.get("event") != event or not isinstance(item[event], dict):
                raise VerificationError("protocol fixture has an invalid production-shaped message")
            if event == "media":
                media = item["media"]
                if set(media) != {"payload"} or not isinstance(media["payload"], str):
                    raise VerificationError("protocol fixture media must contain only a payload")
                try:
                    audio = base64.b64decode(media["payload"], validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise VerificationError("protocol fixture media must be valid base64") from exc
                if audio != b"\xff" * 160:
                    raise VerificationError("protocol fixture media must be one synthetic ulaw silence frame")
            else:
                _safe_protocol_value(item[event])
            messages.append({"event": event, event: dict(item[event])})
        start = messages[1]["start"]
        required_start = {"callId", "otherLegCallId", "callerIdNumber", "calleeIdNumber", "accountId", "mediaFormat"}
        if set(start) != required_start or start["mediaFormat"] != {"encoding": "g711_ulaw", "sampleRate": 8000}:
            raise VerificationError("protocol fixture start must be production-shaped g711 ulaw")
        if terminal_path == "hangup":
            hangup = messages[3]["hangup"]
            if hangup.get("callId") != start["callId"] or hangup.get("otherLegCallId") != start["otherLegCallId"]:
                raise VerificationError("protocol fixture hangup must match the start context")
        scenarios[terminal_path] = tuple(messages)
    return scenarios


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
) -> VerificationReport:
    """Verify static contracts; CI lifecycle proof is owned by a fixed runner.

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
    return VerificationReport(
        agent_slug=resources.slug,
        artifact_digest=artifact_digest,
        template_version=template_version,
        source_revision=source_revision,
        ci_identifier=resources.ci_identifier,
        protocol_events=_REQUIRED_EVENTS,
        static_contracts_passed=True,
        runtime_lifecycle_verified=False,
        ready_for_pr=False,
        runtime_status="CI_LIFECYCLE_REQUIRED",
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
    if not rendered.endswith("\n") or "\n\n" in rendered or _SECRET_OR_CONTROL.search(rendered):
        raise VerificationError("safe report rendering failed")
    return rendered
