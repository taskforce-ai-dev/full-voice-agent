"""Fail-closed Cloudflare Access owner verification boundary."""

from __future__ import annotations

from dataclasses import dataclass
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
        issuer, audience, subject, email, token_type = (
            claims.get("iss"),
            claims.get("aud"),
            claims.get("sub"),
            claims.get("email"),
            claims.get("type"),
        )
        if (
            issuer != self._issuer
            or audience != self._audience
            or token_type != "app"
            or not all(isinstance(value, str) and value for value in (subject, email))
        ):
            raise AccessDenied("Cloudflare Access token was rejected")
        return AccessIdentity(subject=subject, email=email, audience=audience, issuer=issuer)


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
        if identity.subject != self._config.owner_subject or identity.email != self._config.owner_email:
            raise AccessDenied("Cloudflare Access identity is not the factory owner")
        return AccessIdentity(
            subject=identity.subject, email=identity.email,
            audience=identity.audience, issuer=identity.issuer,
        )
