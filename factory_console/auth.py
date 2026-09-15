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
    email: str
    audience: str
    issuer: str


class AccessJWTVerifier(Protocol):
    def verify(self, token: str) -> AccessIdentity: ...


@dataclass(frozen=True)
class CloudflareAccessConfig:
    """Non-secret configuration for a single factory-console owner."""

    expected_audience: str
    expected_issuer: str
    owner_subject: str
    owner_email: str
    jwt_verifier: AccessJWTVerifier | None

    def validate_for_production(self) -> None:
        if not isinstance(self.expected_audience, str) or not self.expected_audience.strip():
            raise AccessConfigurationError("Cloudflare Access audience is required")
        if not isinstance(self.expected_issuer, str) or not self.expected_issuer.strip():
            raise AccessConfigurationError("Cloudflare Access issuer is required")
        if not isinstance(self.owner_subject, str) or not self.owner_subject.strip():
            raise AccessConfigurationError("factory owner subject is required")
        if not isinstance(self.owner_email, str) or not self.owner_email.strip():
            raise AccessConfigurationError("factory owner email is required")
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
        if not access_assertion or access_assertion.strip() != access_assertion:
            raise AccessDenied("owner authorization is required")
        try:
            identity = self._config.jwt_verifier.verify(access_assertion)
        except Exception as error:
            raise AccessDenied("Cloudflare Access token was rejected") from error
        if not isinstance(identity, AccessIdentity) and not (
            isinstance(getattr(identity, "subject", None), str)
            and isinstance(getattr(identity, "email", None), str)
            and isinstance(getattr(identity, "audience", None), str)
            and isinstance(getattr(identity, "issuer", None), str)
        ):
            raise AccessDenied("Cloudflare Access identity is invalid")
        if identity.issuer != self._config.expected_issuer:
            raise AccessDenied("Cloudflare Access issuer is invalid")
        if identity.audience != self._config.expected_audience:
            raise AccessDenied("Cloudflare Access audience is invalid")
        if identity.subject != self._config.owner_subject or identity.email != self._config.owner_email:
            raise AccessDenied("Cloudflare Access identity is not the factory owner")
        return AccessIdentity(
            subject=identity.subject, email=identity.email,
            audience=identity.audience, issuer=identity.issuer,
        )
