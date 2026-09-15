"""Fail-closed validation for the Factory Console's non-secret policy."""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import ip_address
from urllib.parse import urlsplit


_REQUIRED_TOP_LEVEL = {
    "schema_version",
    "origin",
    "cloudflare_access",
    "requests",
    "operations",
    "audit",
}
_FORBIDDEN_ACTIONS = frozenset({"deploy", "provision"})
_SUPPORTED_ACTIONS = frozenset(
    {"inspect", "plan", "approve-knowledge", "approve-plan", "generate", "verify", "open-pr"}
)


def _mapping(value: object, label: str, errors: list[str]) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{label} must be an object")
        return None
    return value


def _exact_keys(value: Mapping[str, object], label: str, required: set[str], errors: list[str]) -> None:
    if set(value) != required:
        errors.append(f"{label} has an invalid schema")


def _nonempty_text(value: object, label: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        errors.append(f"{label} must be non-empty text")
        return None
    return value.strip()


def _absolute_https_url(value: object, label: str, errors: list[str]) -> str | None:
    text = _nonempty_text(value, label, errors)
    if text is None:
        return None
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        errors.append(f"{label} must be an absolute HTTPS URL")
        return None
    return text.rstrip("/")


def validate_policy(raw: object) -> tuple[str, ...]:
    """Return every structural policy violation without accepting a weak default.

    This intentionally validates only non-secret deployment policy. Runtime JWT
    verification remains an application responsibility and must fail closed.
    """
    errors: list[str] = []
    policy = _mapping(raw, "policy", errors)
    if policy is None:
        return tuple(errors)
    _exact_keys(policy, "policy", _REQUIRED_TOP_LEVEL, errors)
    if policy.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    origin = _mapping(policy.get("origin"), "origin", errors)
    if origin is not None:
        _exact_keys(origin, "origin", {"host", "port"}, errors)
        host = _nonempty_text(origin.get("host"), "origin.host", errors)
        if host is not None:
            try:
                if not ip_address(host).is_loopback:
                    errors.append("origin.host must be a loopback address")
            except ValueError:
                errors.append("origin.host must be a loopback address")
        port = origin.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            errors.append("origin.port must be a valid TCP port")

    access = _mapping(policy.get("cloudflare_access"), "cloudflare_access", errors)
    if access is not None:
        _exact_keys(
            access,
            "cloudflare_access",
            {
                "jwt_header",
                "issuer",
                "jwks_url",
                "audience",
                "owner_subject",
                "owner_email",
                "require_application_token",
                "require_exact_owner_match",
            },
            errors,
        )
        if access.get("jwt_header") != "Cf-Access-Jwt-Assertion":
            errors.append("cloudflare_access.jwt_header must be Cf-Access-Jwt-Assertion")
        issuer = _absolute_https_url(access.get("issuer"), "cloudflare_access.issuer", errors)
        jwks_url = _absolute_https_url(access.get("jwks_url"), "cloudflare_access.jwks_url", errors)
        if issuer is not None and not urlsplit(issuer).hostname.endswith(".cloudflareaccess.com"):
            errors.append("cloudflare_access.issuer must be a Cloudflare Access team domain")
        if issuer is not None and jwks_url is not None and jwks_url != f"{issuer}/cdn-cgi/access/certs":
            errors.append("cloudflare_access.jwks_url must be the issuer JWKS endpoint")
        for name in ("audience", "owner_subject", "owner_email"):
            value = _nonempty_text(access.get(name), f"cloudflare_access.{name}", errors)
            if value is not None and "*" in value:
                errors.append(f"cloudflare_access.{name} must not contain a wildcard")
        if access.get("require_application_token") is not True:
            errors.append("cloudflare_access.require_application_token must be true")
        if access.get("require_exact_owner_match") is not True:
            errors.append("cloudflare_access.require_exact_owner_match must be true")

    requests = _mapping(policy.get("requests"), "requests", errors)
    if requests is not None:
        _exact_keys(requests, "requests", {"max_body_bytes", "uploads_enabled"}, errors)
        body_limit = requests.get("max_body_bytes")
        if not isinstance(body_limit, int) or isinstance(body_limit, bool) or not 1 <= body_limit <= 16_384:
            errors.append("requests.max_body_bytes must be between 1 and 16384")
        if requests.get("uploads_enabled") is not False:
            errors.append("requests.uploads_enabled must be false")

    operations = _mapping(policy.get("operations"), "operations", errors)
    if operations is not None:
        _exact_keys(
            operations,
            "operations",
            {"allowed_actions", "approval_required_actions", "forbidden_actions"},
            errors,
        )
        allowed = operations.get("allowed_actions")
        approvals = operations.get("approval_required_actions")
        forbidden = operations.get("forbidden_actions")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            errors.append("operations.allowed_actions must be a list of actions")
            allowed_set: set[str] = set()
        else:
            allowed_set = set(allowed)
            if len(allowed_set) != len(allowed) or not allowed_set or not allowed_set <= _SUPPORTED_ACTIONS:
                errors.append("operations.allowed_actions contains an unsupported action")
            if allowed_set & _FORBIDDEN_ACTIONS:
                errors.append("operations.allowed_actions contains a forbidden action")
        if not isinstance(approvals, list) or not all(isinstance(item, str) for item in approvals):
            errors.append("operations.approval_required_actions must be a list of actions")
            approval_set: set[str] = set()
        else:
            approval_set = set(approvals)
            if len(approval_set) != len(approvals) or not approval_set <= allowed_set:
                errors.append("operations.approval_required_actions must be allowed actions")
        for action in ("generate", "open-pr"):
            if action not in approval_set:
                errors.append(f"operations.approval_required_actions must include {action}")
        if not isinstance(forbidden, list) or set(forbidden) != _FORBIDDEN_ACTIONS:
            errors.append("operations.forbidden_actions must exactly deny deploy and provision")

    audit = _mapping(policy.get("audit"), "audit", errors)
    if audit is not None:
        _exact_keys(
            audit,
            "audit",
            {"sink", "include_request_content", "include_authorization_headers", "include_token_claims"},
            errors,
        )
        if audit.get("sink") != "journal":
            errors.append("audit.sink must be journal")
        for name in ("include_request_content", "include_authorization_headers", "include_token_claims"):
            if audit.get(name) is not False:
                errors.append(f"audit.{name} must be false")

    return tuple(errors)
