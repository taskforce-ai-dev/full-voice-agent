"""Fail-closed Cloudflare Access owner verification boundary."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
from typing import Callable, Mapping, Protocol


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
    role: str = "unclassified"


@dataclass(frozen=True)
class ReviewerIdentity:
    """One non-secret Cloudflare Access identity allowed to review test intake."""

    subject: str
    email: str


class AccessJWTVerifier(Protocol):
    def verify(self, token: str) -> AccessIdentity: ...


class PyJWTAccessJWTVerifier:
    """Cryptographically verify a Cloudflare Access application assertion."""

    def __init__(
        self,
        *,
        jwks_url: str,
        expected_issuer: str,
        expected_audience: str,
        cache_seconds: int = 300,
        clock_skew_seconds: int = 60,
        jwks_timeout_seconds: int = 5,
        max_cached_keys: int = 16,
        jwks_client: object | None = None,
        decoder: Callable[..., Mapping[str, object]] | None = None,
    ) -> None:
        if not all(isinstance(value, str) and value for value in (jwks_url, expected_issuer, expected_audience)):
            raise AccessConfigurationError("Cloudflare Access verifier settings are required")
        if not isinstance(cache_seconds, int) or not 60 <= cache_seconds <= 900:
            raise AccessConfigurationError("Cloudflare Access JWKS cache lifetime is invalid")
        if not isinstance(clock_skew_seconds, int) or not 0 <= clock_skew_seconds <= 120:
            raise AccessConfigurationError("Cloudflare Access clock skew is invalid")
        if not isinstance(jwks_timeout_seconds, int) or not 1 <= jwks_timeout_seconds <= 10:
            raise AccessConfigurationError("Cloudflare Access JWKS timeout is invalid")
        if not isinstance(max_cached_keys, int) or not 1 <= max_cached_keys <= 16:
            raise AccessConfigurationError("Cloudflare Access JWKS key cache is invalid")
        if jwks_client is None or decoder is None:
            try:
                import jwt
            except ImportError as error:
                raise AccessConfigurationError("PyJWT runtime dependency is unavailable") from error
            jwks_client = jwks_client or jwt.PyJWKClient(
                jwks_url,
                cache_keys=True,
                max_cached_keys=max_cached_keys,
                cache_jwk_set=True,
                lifespan=cache_seconds,
                timeout=jwks_timeout_seconds,
            )
            decoder = decoder or jwt.decode
        self._jwks_client = jwks_client
        self._decoder = decoder
        self._issuer = expected_issuer
        self._audience = expected_audience
        self._clock_skew_seconds = clock_skew_seconds

    def verify(self, token: str) -> AccessIdentity:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = self._decoder(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._clock_skew_seconds,
                options={"require": ["aud", "email", "exp", "iat", "iss", "nbf", "sub", "type"]},
            )
        except Exception as error:
            raise AccessDenied("Cloudflare Access token was rejected") from error
        if not isinstance(claims, Mapping):
            raise AccessDenied("Cloudflare Access token was rejected")
        issuer, audiences, subject, email, token_type = (
            claims.get("iss"),
            claims.get("aud"),
            claims.get("sub"),
            claims.get("email"),
            claims.get("type"),
        )
        valid_audience = (
            isinstance(audiences, (list, tuple))
            and bool(audiences)
            and all(isinstance(value, str) and value for value in audiences)
            and self._audience in audiences
        )
        if (
            issuer != self._issuer
            or not valid_audience
            or token_type != "app"
            or not all(isinstance(value, str) and value for value in (subject, email))
        ):
            raise AccessDenied("Cloudflare Access token was rejected")
        return AccessIdentity(subject=subject, email=email, audience=self._audience, issuer=issuer)


@dataclass(frozen=True)
class CloudflareAccessConfig:
    """Non-secret configuration for the owner and optional bounded reviewers."""

    expected_audience: str
    expected_issuer: str
    owner_subject: str
    owner_email: str
    jwt_verifier: AccessJWTVerifier | None
    reviewers: tuple[ReviewerIdentity, ...] = ()

    def validate_for_production(self) -> None:
        if not isinstance(self.expected_audience, str) or not self.expected_audience.strip():
            raise AccessConfigurationError("Cloudflare Access audience is required")
        if not isinstance(self.expected_issuer, str) or not self.expected_issuer.strip():
            raise AccessConfigurationError("Cloudflare Access issuer is required")
        if not _valid_identity_text(self.owner_subject):
            raise AccessConfigurationError("factory owner subject is required")
        if not _valid_identity_text(self.owner_email):
            raise AccessConfigurationError("factory owner email is required")
        if self.jwt_verifier is None or not callable(getattr(self.jwt_verifier, "verify", None)):
            raise AccessConfigurationError("Cloudflare Access JWT verifier is required")
        if not isinstance(self.reviewers, tuple) or len(self.reviewers) > 16:
            raise AccessConfigurationError("factory reviewer identities are invalid")
        pairs: set[tuple[str, str]] = set()
        for reviewer in self.reviewers:
            if not isinstance(reviewer, ReviewerIdentity) or not (
                _valid_identity_text(reviewer.subject) and _valid_identity_text(reviewer.email)
            ):
                raise AccessConfigurationError("factory reviewer identities are invalid")
            if reviewer.subject == self.owner_subject or reviewer.email == self.owner_email:
                raise AccessConfigurationError("factory reviewer identities must be distinct from the owner")
            pair = (reviewer.subject, reviewer.email)
            if pair in pairs:
                raise AccessConfigurationError("factory reviewer identities must be distinct")
            pairs.add(pair)


def _valid_identity_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "\x00" not in value
        and "*" not in value
        and len(value) <= 512
    )


class CloudflareAccessVerifier:
    """Classify only a signed Access owner or configured exact reviewer pair."""

    def __init__(self, config: CloudflareAccessConfig) -> None:
        config.validate_for_production()
        self._config = config

    def require_identity(self, headers: Mapping[str, str]) -> AccessIdentity:
        access_assertion = next(
            (value for name, value in headers.items() if name.lower() == "cf-access-jwt-assertion"), ""
        )
        if not access_assertion or access_assertion.strip() != access_assertion or "," in access_assertion:
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
        if _matches_pair(identity.subject, identity.email, self._config.owner_subject, self._config.owner_email):
            role = "owner"
        elif any(
            _matches_pair(identity.subject, identity.email, reviewer.subject, reviewer.email)
            for reviewer in self._config.reviewers
        ):
            role = "reviewer"
        else:
            raise AccessDenied("Cloudflare Access identity is not authorized for the factory console")
        return AccessIdentity(
            subject=identity.subject, email=identity.email,
            audience=identity.audience, issuer=identity.issuer, role=role,
        )

    def require_owner(self, headers: Mapping[str, str]) -> AccessIdentity:
        """Compatibility boundary for owner-only callers and tests."""
        identity = self.require_identity(headers)
        if identity.role != "owner":
            raise AccessDenied("Cloudflare Access identity is not the factory owner")
        return identity


def _matches_pair(subject: str, email: str, expected_subject: str, expected_email: str) -> bool:
    return (
        hmac.compare_digest(subject.encode("utf-8"), expected_subject.encode("utf-8"))
        and hmac.compare_digest(email.encode("utf-8"), expected_email.encode("utf-8"))
    )
