"""Small dependency-free WSGI API; it contains no deploy/provision capability."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Protocol

from .auth import AccessDenied, AccessIdentity, CloudflareAccessVerifier
from .domain import ConsoleJobService, FactoryConsoleJob, IntakeValidationError, InvalidStateTransition


_MAX_BODY_BYTES = 16 * 1024


class CSRFTokenVerifier(Protocol):
    """Server-side validator for an owner-, method-, and path-bound CSRF token."""

    def verify(self, *, token: str, owner_identity: str, method: str, path: str) -> bool: ...


@dataclass(frozen=True)
class CSRFConfig:
    expected_origin: str
    token_verifier: CSRFTokenVerifier | None

    def validate_for_production(self) -> None:
        if not isinstance(self.expected_origin, str) or not self.expected_origin.startswith("https://"):
            raise ValueError("an HTTPS console origin is required")
        if self.token_verifier is None or not callable(getattr(self.token_verifier, "verify", None)):
            raise ValueError("a server-side CSRF token verifier is required")


class CSRFProtection:
    """Require exact origin and server-validated CSRF confirmation for writes."""

    def __init__(self, config: CSRFConfig) -> None:
        config.validate_for_production()
        self._config = config

    def require_request(
        self, identity: AccessIdentity, method: str, path: str, headers: Mapping[str, str]
    ) -> None:
        if headers.get("Origin") != self._config.expected_origin:
            raise AccessDenied("request origin or CSRF token rejected")
        token = headers.get("X-Factory-Console-CSRF", "")
        try:
            valid = self._config.token_verifier.verify(
                token=token, owner_identity=identity.subject, method=method, path=path
            )
        except Exception as error:
            raise AccessDenied("request origin or CSRF token rejected") from error
        if valid is not True:
            raise AccessDenied("request origin or CSRF token rejected")


class FactoryConsoleWSGIApp:
    def __init__(
        self, jobs: ConsoleJobService, access: CloudflareAccessVerifier, csrf: CSRFProtection
    ) -> None:
        self._jobs = jobs
        self._access = access
        self._csrf = csrf

    def __call__(self, environ: Mapping[str, object], start_response: Callable) -> Iterable[bytes]:
        try:
            identity = self._access.require_owner(self._headers(environ))
        except AccessDenied:
            return self._respond(start_response, "403 Forbidden", {"error": "owner authorization required"})
        method = environ.get("REQUEST_METHOD")
        path = environ.get("PATH_INFO")
        if not isinstance(method, str) or not isinstance(path, str):
            return self._respond(start_response, "400 Bad Request", {"error": "invalid request"})
        if method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                self._csrf.require_request(identity, method, path, self._headers(environ))
            except AccessDenied:
                return self._respond(start_response, "403 Forbidden", {"error": "request origin or CSRF token rejected"})
        try:
            if method == "POST" and path == "/v1/jobs":
                return self._respond(start_response, "201 Created", self._view(self._jobs.create(self._body(environ))))
            if method == "GET" and path.startswith("/v1/jobs/") and path.count("/") == 3:
                return self._respond(start_response, "200 OK", self._view(self._jobs.get(path.rsplit("/", 1)[-1])))
            action = self._action(path, method)
            if action is not None:
                job_id, operation = action
                if operation in {"approve_knowledge", "approve_plan"}:
                    job = getattr(self._jobs, operation)(job_id, self._confirmation(self._body(environ)))
                else:
                    job = getattr(self._jobs, operation)(job_id)
                return self._respond(start_response, "200 OK", self._view(job))
        except (IntakeValidationError, InvalidStateTransition, ValueError) as error:
            return self._respond(start_response, "400 Bad Request", {"error": str(error)})
        except KeyError:
            return self._respond(start_response, "404 Not Found", {"error": "not found"})
        except Exception:
            return self._respond(start_response, "500 Internal Server Error", {"error": "factory console request failed"})
        return self._respond(start_response, "404 Not Found", {"error": "not found"})

    @staticmethod
    def _headers(environ: Mapping[str, object]) -> dict[str, str]:
        headers: dict[str, str] = {}
        if isinstance(environ.get("HTTP_CF_ACCESS_JWT_ASSERTION"), str):
            headers["Cf-Access-Jwt-Assertion"] = environ["HTTP_CF_ACCESS_JWT_ASSERTION"]
        if isinstance(environ.get("HTTP_ORIGIN"), str):
            headers["Origin"] = environ["HTTP_ORIGIN"]
        if isinstance(environ.get("HTTP_X_FACTORY_CONSOLE_CSRF"), str):
            headers["X-Factory-Console-CSRF"] = environ["HTTP_X_FACTORY_CONSOLE_CSRF"]
        return headers

    @staticmethod
    def _action(path: str, method: str) -> tuple[str, str] | None:
        if method != "POST":
            return None
        parts = path.split("/")
        if len(parts) != 5 or parts[:3] != ["", "v1", "jobs"]:
            return None
        operations = {
            "inspect": "inspect",
            "plan": "prepare_plan",
            "approve-knowledge": "approve_knowledge",
            "approve-plan": "approve_plan",
            "generate": "generate",
            "verify": "verify",
            "open-pr": "open_pr",
        }
        operation = operations.get(parts[4])
        return (parts[3], operation) if operation else None

    @staticmethod
    def _body(environ: Mapping[str, object]) -> Mapping[str, object]:
        try:
            length = int(environ.get("CONTENT_LENGTH", "0"))
        except (TypeError, ValueError) as error:
            raise ValueError("invalid content length") from error
        if not 0 <= length <= _MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        stream = environ.get("wsgi.input")
        read = getattr(stream, "read", None)
        if not callable(read):
            raise ValueError("request body is unavailable")
        try:
            payload = json.loads(read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    @staticmethod
    def _confirmation(payload: Mapping[str, object]) -> str:
        if set(payload) != {"digest"} or not isinstance(payload["digest"], str):
            raise ValueError("exact review digest confirmation is required")
        return payload["digest"]

    @staticmethod
    def _view(job: FactoryConsoleJob) -> dict[str, object]:
        return {
            "job_id": job.job_id,
            "state": job.state.value,
            "intake": {
                "company_name": job.intake.company_name,
                "industry": job.intake.industry,
                "purpose": job.intake.purpose,
                "primary_contact": job.intake.primary_contact,
                "supported_languages": list(job.intake.supported_languages),
            },
            "review": FactoryConsoleWSGIApp._review(job),
        }

    @staticmethod
    def _review(job: FactoryConsoleJob) -> dict[str, str] | None:
        generation = job.generation
        if generation is None:
            return None
        if generation.stage.value == "KNOWLEDGE_REVIEW_REQUIRED" and generation.knowledge_review_digest:
            return {"status": "knowledge_review_required", "digest": generation.knowledge_review_digest}
        if generation.stage.value == "PLAN_REVIEW_REQUIRED" and generation.plan_digest:
            return {"status": "plan_review_required", "digest": generation.plan_digest}
        return None

    @staticmethod
    def _respond(start_response: Callable, status: str, payload: Mapping[str, object]) -> list[bytes]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
        return [body]
