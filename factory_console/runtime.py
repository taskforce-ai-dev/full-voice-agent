"""Root-owned production construction for the review-only WSGI console."""

from __future__ import annotations

import grp
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from factory_console_hardening.policy import validate_policy
from smartpbx_agent_factory.bootstrap import FactoryBootstrap, FactoryConfig, FactoryConfigError, inspect_config

from .api import CSRFConfig, CSRFProtection, FactoryConsoleWSGIApp
from .auth import CloudflareAccessConfig, CloudflareAccessVerifier, PyJWTAccessJWTVerifier, ReviewerIdentity
from .csrf import HMACCSRFTokenVerifier
from .domain import CompanyIntake, ConsoleJobService, FactoryOperationBlocked
from .factory import InternalManifestResolver, SmartPBXFactoryAdapter


_RUNTIME_CONFIG_PATH = Path("/etc/factory-console/runtime.json")
_RUNTIME_KEYS = {"version", "policy_path", "factory_config_path", "csrf_secret_file", "manifests"}


class RuntimeConfigurationError(RuntimeError):
    """A root-owned runtime setting is missing, unsafe, or inconsistent."""


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimeConfigurationError(f"{label} is required")
    path = Path(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeConfigurationError(f"{label} is invalid")
    return path


def _root_owned_regular(path: Path, stat_for_path: Callable[[Path], os.stat_result]) -> os.stat_result:
    try:
        metadata = stat_for_path(path)
    except OSError as error:
        raise RuntimeConfigurationError("a required root-owned runtime file is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeConfigurationError("a required root-owned runtime file is unsafe")
    return metadata


def _factory_console_group_gid() -> int:
    try:
        return grp.getgrnam("factory-console").gr_gid
    except KeyError as error:
        raise RuntimeConfigurationError("factory-console service group is unavailable") from error


def _service_readable_csrf_secret(
    path: Path, stat_for_path: Callable[[Path], os.stat_result], service_gid: int
) -> None:
    metadata = _root_owned_regular(path, stat_for_path)
    mode = metadata.st_mode
    if (
        metadata.st_gid != service_gid
        or not mode & stat.S_IRGRP
        or mode & (stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeConfigurationError("CSRF signing secret must be root-owned and service-group readable")


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeConfigurationError("a required runtime JSON file is invalid") from error


@dataclass(frozen=True)
class RuntimeConfig:
    policy_path: Path
    factory_config_path: Path
    csrf_secret_file: Path
    manifests: Mapping[str, Path]


def load_runtime_config(
    path: str | Path = _RUNTIME_CONFIG_PATH,
    *,
    stat_for_path: Callable[[Path], os.stat_result] = os.lstat,
    service_gid: int | None = None,
) -> RuntimeConfig:
    config_path = _absolute_path(str(path), "runtime config path")
    _root_owned_regular(config_path, stat_for_path)
    raw = _load_json(config_path)
    if not isinstance(raw, dict) or set(raw) != _RUNTIME_KEYS or raw.get("version") != 1:
        raise RuntimeConfigurationError("runtime config has an invalid schema")
    policy_path = _absolute_path(raw["policy_path"], "policy path")
    factory_config_path = _absolute_path(raw["factory_config_path"], "factory config path")
    csrf_secret_file = _absolute_path(raw["csrf_secret_file"], "CSRF secret path")
    for required_path in (policy_path, factory_config_path):
        _root_owned_regular(required_path, stat_for_path)
    if service_gid is None:
        service_gid = _factory_console_group_gid()
    if not isinstance(service_gid, int) or isinstance(service_gid, bool) or service_gid < 0:
        raise RuntimeConfigurationError("factory-console service group is invalid")
    _service_readable_csrf_secret(csrf_secret_file, stat_for_path, service_gid)
    manifests = raw["manifests"]
    if not isinstance(manifests, dict) or not manifests:
        raise RuntimeConfigurationError("at least one server-owned manifest is required")
    resolved: dict[str, Path] = {}
    for company_name, manifest_path in manifests.items():
        if not isinstance(company_name, str) or not company_name.strip() or company_name != company_name.strip():
            raise RuntimeConfigurationError("manifest company names are invalid")
        resolved[company_name] = _absolute_path(manifest_path, "manifest path")
    return RuntimeConfig(policy_path, factory_config_path, csrf_secret_file, resolved)


def _concrete_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "REPLACE_WITH" not in value and "<" not in value and ">" not in value


def _load_valid_policy(config: RuntimeConfig) -> Mapping[str, object]:
    policy = _load_json(config.policy_path)
    if validate_policy(policy):
        raise RuntimeConfigurationError("factory console hardening policy is invalid")
    assert isinstance(policy, Mapping)
    access = policy["cloudflare_access"]
    runtime = policy["runtime"]
    assert isinstance(access, Mapping) and isinstance(runtime, Mapping)
    if not all(_concrete_text(access[name]) for name in ("issuer", "jwks_url", "audience", "owner_subject", "owner_email")):
        raise RuntimeConfigurationError("factory console Access policy still has placeholders")
    reviewer_identities = access["reviewer_identities"]
    if not isinstance(reviewer_identities, list) or any(
        not isinstance(reviewer, Mapping)
        or not all(_concrete_text(reviewer.get(name)) for name in ("subject", "email"))
        for reviewer in reviewer_identities
    ):
        raise RuntimeConfigurationError("factory console reviewer identities are invalid")
    if not _concrete_text(runtime["ui_origin"]):
        raise RuntimeConfigurationError("factory console UI origin is invalid")
    parsed_origin = urlsplit(str(runtime["ui_origin"]))
    if parsed_origin.scheme != "https" or not parsed_origin.hostname or parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
        raise RuntimeConfigurationError("factory console UI origin is invalid")
    origin = policy["origin"]
    assert isinstance(origin, Mapping)
    if origin.get("host") != "127.0.0.1" or origin.get("port") != 8401:
        raise RuntimeConfigurationError("hardening policy and loopback runtime disagree")
    return policy


def _load_csrf_secret(config: RuntimeConfig) -> bytes:
    try:
        secret = config.csrf_secret_file.read_bytes().rstrip(b"\r\n")
    except OSError as error:
        raise RuntimeConfigurationError("CSRF signing secret is unavailable") from error
    if not 32 <= len(secret) <= 256:
        raise RuntimeConfigurationError("CSRF signing secret is invalid")
    return secret


class ConfiguredManifestResolver(InternalManifestResolver):
    def __init__(self, manifests: Mapping[str, Path], approved_roots: tuple[Path, ...]) -> None:
        self._manifests = dict(manifests)
        self._approved_roots = tuple(root.resolve() for root in approved_roots)
        for path in self._manifests.values():
            try:
                resolved = path.resolve(strict=True)
            except OSError as error:
                raise RuntimeConfigurationError("a configured factory manifest is unavailable") from error
            if not resolved.is_file() or path.is_symlink() or not any(resolved.is_relative_to(root) for root in self._approved_roots):
                raise RuntimeConfigurationError("a configured factory manifest is unsafe")
            if resolved != path:
                raise RuntimeConfigurationError("a configured factory manifest must be canonical")

    def manifest_for(self, intake: CompanyIntake) -> Path:
        try:
            return self._manifests[intake.company_name]
        except KeyError as error:
            raise FactoryOperationBlocked("configured factory manifest is unavailable") from error


def build_application(path: str | Path = _RUNTIME_CONFIG_PATH) -> FactoryConsoleWSGIApp:
    config = load_runtime_config(path)
    policy = _load_valid_policy(config)
    try:
        factory_config = FactoryConfig.load(config.factory_config_path)
    except FactoryConfigError as error:
        raise RuntimeConfigurationError("review-only factory config is invalid") from error
    checks = inspect_config(factory_config)
    if not checks or any(result != "ready" for result in checks.values()):
        raise RuntimeConfigurationError("review-only factory prerequisites are not ready")
    access = policy["cloudflare_access"]
    runtime = policy["runtime"]
    assert isinstance(access, Mapping) and isinstance(runtime, Mapping)
    jwt_verifier = PyJWTAccessJWTVerifier(
        jwks_url=str(access["jwks_url"]),
        expected_issuer=str(access["issuer"]),
        expected_audience=str(access["audience"]),
        cache_seconds=int(runtime["jwks_cache_seconds"]),
        clock_skew_seconds=int(runtime["jwt_clock_skew_seconds"]),
        jwks_timeout_seconds=int(runtime["jwks_timeout_seconds"]),
        max_cached_keys=int(runtime["max_cached_jwks"]),
    )
    csrf = HMACCSRFTokenVerifier(_load_csrf_secret(config), ttl_seconds=int(runtime["csrf_ttl_seconds"]))
    return FactoryConsoleWSGIApp(
        ConsoleJobService(SmartPBXFactoryAdapter(
            FactoryBootstrap(factory_config),
            ConfiguredManifestResolver(config.manifests, factory_config.approved_source_roots),
        )),
        CloudflareAccessVerifier(CloudflareAccessConfig(
            expected_audience=str(access["audience"]),
            expected_issuer=str(access["issuer"]),
            owner_subject=str(access["owner_subject"]),
            owner_email=str(access["owner_email"]),
            jwt_verifier=jwt_verifier,
            reviewers=tuple(
                ReviewerIdentity(str(reviewer["subject"]), str(reviewer["email"]))
                for reviewer in access["reviewer_identities"]
            ),
        )),
        CSRFProtection(CSRFConfig(expected_origin=str(runtime["ui_origin"]).rstrip("/"), token_verifier=csrf)),
    )


def verify_runtime(path: str | Path = _RUNTIME_CONFIG_PATH) -> None:
    build_application(path)
