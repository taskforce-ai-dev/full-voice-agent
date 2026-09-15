"""Fail-closed Cloudflare Access owner verification boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


class AccessConfigurationError(RuntimeError):
    """Cloudflare Access verification has not been configured safely."""


class AccessDenied(PermissionError):
    """The request did not prove the configured owner identity."""


@dataclass(frozen=True)
class AccessIdentity:
    subject: str
    audience: str


class AccessJWTVerifier(Protocol):
    def verify(self, token: str) -> AccessIdentity: ...


@dataclass(frozen=True)
class CloudflareAccessConfig:
    """Non-secret configuration for a single factory-console owner."""

    expected_audience: str
    owner_identity: str
    jwt_verifier: AccessJWTVerifier | None

    def validate_for_production(self) -> None:
        if not isinstance(self.expected_audience, str) or not self.expected_audience.strip():
            raise AccessConfigurationError("Cloudflare Access audience is required")
        if not isinstance(self.owner_identity, str) or not self.owner_identity.strip():
            raise AccessConfigurationError("factory owner identity is required")
        if self.jwt_verifier is None or not callable(getattr(self.jwt_verifier, "verify", None)):
            raise AccessConfigurationError("Cloudflare Access JWT verifier is required")


class CloudflareAccessVerifier:
    """Accept only a validated JWT for the configured Cloudflare Access owner."""

    def __init__(self, config: CloudflareAccessConfig) -> None:
        config.validate_for_production()
        self._config = config

    def require_owner(self, headers: Mapping[str, str]) -> AccessIdentity:
        access_assertion = next(
            (value for name, value in headers.items() if name.lower() == "cf-access-jwt-assertion"), ""
        )
        authorization = next(
            (value for name, value in headers.items() if name.lower() == "authorization"), ""
        )
        scheme, _, bearer_token = authorization.partition(" ")
        token = access_assertion or bearer_token
        if (not access_assertion and scheme.lower() != "bearer") or not token or token.strip() != token:
            raise AccessDenied("owner authorization is required")
        try:
            identity = self._config.jwt_verifier.verify(token)
        except Exception as error:
            raise AccessDenied("Cloudflare Access token was rejected") from error
        if not isinstance(identity, AccessIdentity) and not (
            isinstance(getattr(identity, "subject", None), str)
            and isinstance(getattr(identity, "audience", None), str)
        ):
            raise AccessDenied("Cloudflare Access identity is invalid")
        if identity.audience != self._config.expected_audience:
            raise AccessDenied("Cloudflare Access audience is invalid")
        if identity.subject != self._config.owner_identity:
            raise AccessDenied("Cloudflare Access identity is not the factory owner")
        return AccessIdentity(subject=identity.subject, audience=identity.audience)
