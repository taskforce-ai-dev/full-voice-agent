"""Small dependency-free WSGI API; it contains no deploy/provision capability."""

from __future__ import annotations

import json
from typing import Callable, Iterable, Mapping

from .auth import AccessDenied, CloudflareAccessVerifier
from .domain import ConsoleJobService, FactoryConsoleJob, IntakeValidationError, InvalidStateTransition


_MAX_BODY_BYTES = 16 * 1024


class FactoryConsoleWSGIApp:
    def __init__(self, jobs: ConsoleJobService, access: CloudflareAccessVerifier) -> None:
        self._jobs = jobs
        self._access = access

    def __call__(self, environ: Mapping[str, object], start_response: Callable) -> Iterable[bytes]:
        try:
            self._access.require_owner(self._headers(environ))
        except AccessDenied:
            return self._respond(start_response, "403 Forbidden", {"error": "owner authorization required"})
        method = environ.get("REQUEST_METHOD")
        path = environ.get("PATH_INFO")
        if not isinstance(method, str) or not isinstance(path, str):
            return self._respond(start_response, "400 Bad Request", {"error": "invalid request"})
        try:
            if method == "POST" and path == "/v1/jobs":
                return self._respond(start_response, "201 Created", self._view(self._jobs.create(self._body(environ))))
            if method == "GET" and path.startswith("/v1/jobs/") and path.count("/") == 3:
                return self._respond(start_response, "200 OK", self._view(self._jobs.get(path.rsplit("/", 1)[-1])))
            action = self._action(path, method)
            if action is not None:
                job_id, operation = action
                return self._respond(start_response, "200 OK", self._view(getattr(self._jobs, operation)(job_id)))
        except (IntakeValidationError, InvalidStateTransition, ValueError) as error:
            return self._respond(start_response, "400 Bad Request", {"error": str(error)})
        except KeyError:
            return self._respond(start_response, "404 Not Found", {"error": "not found"})
        return self._respond(start_response, "404 Not Found", {"error": "not found"})

    @staticmethod
    def _headers(environ: Mapping[str, object]) -> dict[str, str]:
        headers: dict[str, str] = {}
        if isinstance(environ.get("HTTP_AUTHORIZATION"), str):
            headers["Authorization"] = environ["HTTP_AUTHORIZATION"]
        if isinstance(environ.get("HTTP_CF_ACCESS_JWT_ASSERTION"), str):
            headers["Cf-Access-Jwt-Assertion"] = environ["HTTP_CF_ACCESS_JWT_ASSERTION"]
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
            "approve": "approve_for_generation",
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
        }

    @staticmethod
    def _respond(start_response: Callable, status: str, payload: Mapping[str, object]) -> list[bytes]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
        return [body]
