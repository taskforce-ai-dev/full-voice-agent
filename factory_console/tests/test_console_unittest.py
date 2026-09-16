"""Executable security and lifecycle contracts for the factory console."""

from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class _Identity:
    subject: str
    email: str
    audience: str
    issuer: str


class _Verifier:
    def verify(self, token: str) -> _Identity:
        if token == "valid":
            return _Identity(subject="owner-subject", email="owner@example.com", audience="factory-console", issuer="https://access.example")
        if token == "reviewer":
            return _Identity(subject="reviewer-subject", email="reviewer@example.com", audience="factory-console", issuer="https://access.example")
        if token == "wrong-email":
            return _Identity(subject="owner-subject", email="other@example.com", audience="factory-console", issuer="https://access.example")
        return _Identity(subject="other-subject", email="owner@example.com", audience="factory-console", issuer="https://access.example")


class _Factory:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def inspect(self, job):
        self.operations.append("inspect")

    def plan(self, job):
        self.operations.append("plan")
        from smartpbx_agent_factory.state import GenerationState, Stage

        return GenerationState(
            generation_id="gen-" + "a" * 32,
            manifest_digest="b" * 64,
            stage=Stage.KNOWLEDGE_REVIEW_REQUIRED,
            knowledge_review_digest="c" * 64,
        )

    def approve_knowledge(self, job, generation, digest):
        self.operations.append("approve_knowledge")
        generation.approve_knowledge(digest)
        generation.record_plan_digest("d" * 64)
        return generation

    def approve_plan(self, job, generation, digest):
        self.operations.append("approve_plan")
        generation.approve_plan(digest)
        return generation

    def generate(self, job, generation):
        self.operations.append("generate")
        return generation

    def verify(self, job, generation):
        self.operations.append("verify")
        from smartpbx_agent_factory.state import Stage

        generation.transition(Stage.VERIFIED)
        return generation

    def open_pr(self, job, generation):
        self.operations.append("open_pr")
        from smartpbx_agent_factory.state import Stage

        generation.transition(Stage.THREE_PRS_OPENED)
        return generation


class _BlockingFactory(_Factory):
    def plan(self, job):
        from factory_console.domain import FactoryOperationBlocked

        raise FactoryOperationBlocked("provenance validation failed")


class _CSRF:
    def issue(self, *, owner_identity):
        return "csrf-valid"

    def verify(self, *, token, owner_identity, method, path):
        return token == "csrf-valid" and owner_identity == "owner-subject" and method == "POST" and path.startswith("/v1/jobs")


class _ReviewerCSRF:
    def issue(self, *, owner_identity):
        return "reviewer-csrf"

    def verify(self, *, token, owner_identity, method, path):
        return token == "reviewer-csrf" and owner_identity == "reviewer-subject" and method == "POST" and path.startswith("/v1/jobs")


class _SigningKey:
    key = object()


class _JWKS:
    def get_signing_key_from_jwt(self, token):
        if token == "unknown-kid":
            raise ValueError("unknown key")
        return _SigningKey()


def _claims(**overrides):
    claims = {
        "iss": "https://access.example",
        "aud": "factory-console",
        "sub": "owner-subject",
        "email": "owner@example.com",
        "type": "app",
        "exp": 4_000_000_000,
        "iat": 1_000_000_000,
        "nbf": 1_000_000_000,
    }
    claims.update(overrides)
    return claims


class CloudflareAccessTests(unittest.TestCase):
    def test_configured_reviewer_requires_an_exact_verified_identity_pair(self) -> None:
        from factory_console.auth import (
            AccessDenied,
            CloudflareAccessConfig,
            CloudflareAccessVerifier,
            ReviewerIdentity,
        )

        verifier = CloudflareAccessVerifier(CloudflareAccessConfig(
            expected_audience="factory-console",
            expected_issuer="https://access.example",
            owner_subject="owner-subject",
            owner_email="owner@example.com",
            reviewers=(ReviewerIdentity("reviewer-subject", "reviewer@example.com"),),
            jwt_verifier=_Verifier(),
        ))

        identity = verifier.require_identity({"Cf-Access-Jwt-Assertion": "reviewer"})

        self.assertEqual(identity.role, "reviewer")
        self.assertEqual(identity.subject, "reviewer-subject")
        with self.assertRaises(AccessDenied):
            verifier.require_owner({"Cf-Access-Jwt-Assertion": "reviewer"})
        with self.assertRaises(AccessDenied):
            verifier.require_identity({"Cf-Access-Jwt-Assertion": "wrong-email"})

    def test_rejects_missing_or_non_owner_identity(self) -> None:
        from factory_console.auth import AccessDenied, CloudflareAccessConfig, CloudflareAccessVerifier

        verifier = CloudflareAccessVerifier(
            CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="https://access.example",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                jwt_verifier=_Verifier(),
            )
        )

        with self.assertRaises(AccessDenied):
            verifier.require_owner({})
        with self.assertRaises(AccessDenied):
            verifier.require_owner({"Authorization": "Bearer valid"})
        with self.assertRaises(AccessDenied):
            verifier.require_owner({"Cf-Access-Jwt-Assertion": "wrong-email"})
        with self.assertRaises(AccessDenied):
            verifier.require_owner({"Cf-Access-Jwt-Assertion": "valid,second"})

    def test_accepts_a_verified_cloudflare_access_assertion(self) -> None:
        from factory_console.auth import CloudflareAccessConfig, CloudflareAccessVerifier

        verifier = CloudflareAccessVerifier(CloudflareAccessConfig(
            expected_audience="factory-console",
            expected_issuer="https://access.example",
            owner_subject="owner-subject",
            owner_email="owner@example.com",
            jwt_verifier=_Verifier(),
        ))

        identity = verifier.require_owner({"Cf-Access-Jwt-Assertion": "valid"})

        self.assertEqual(identity.subject, "owner-subject")
        self.assertEqual(identity.email, "owner@example.com")

    def test_production_configuration_fails_closed_without_jwt_verifier(self) -> None:
        from factory_console.auth import AccessConfigurationError, CloudflareAccessConfig

        with self.assertRaises(AccessConfigurationError):
            CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="https://access.example",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                jwt_verifier=None,
            ).validate_for_production()
        with self.assertRaises(AccessConfigurationError):
            CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                jwt_verifier=_Verifier(),
            ).validate_for_production()

    def test_pyjwt_verifier_rejects_bad_signature_at_the_verifier_seam(self) -> None:
        from factory_console.auth import (
            AccessDenied,
            CloudflareAccessConfig,
            CloudflareAccessVerifier,
            PyJWTAccessJWTVerifier,
        )

        def rejected_signature(*args, **kwargs):
            raise ValueError("signature rejected")

        verifier = CloudflareAccessVerifier(CloudflareAccessConfig(
            expected_audience="factory-console",
            expected_issuer="https://access.example",
            owner_subject="owner-subject",
            owner_email="owner@example.com",
            jwt_verifier=PyJWTAccessJWTVerifier(
                jwks_url="https://access.example/cdn-cgi/access/certs",
                expected_issuer="https://access.example",
                expected_audience="factory-console",
                jwks_client=_JWKS(),
                decoder=rejected_signature,
            ),
        ))

        with self.assertRaises(AccessDenied):
            verifier.require_owner({"Cf-Access-Jwt-Assertion": "bad-signature"})

    def test_pyjwt_verifier_requires_exact_claims_and_application_type(self) -> None:
        from factory_console.auth import (
            AccessDenied,
            CloudflareAccessConfig,
            CloudflareAccessVerifier,
            PyJWTAccessJWTVerifier,
        )

        for invalid in (
            _claims(iss="https://other.example"),
            _claims(aud="different-audience"),
            _claims(aud=["different-audience"]),
            _claims(aud="factory-console"),
            _claims(sub="other-subject"),
            _claims(email="other@example.com"),
            _claims(email=None),
            _claims(type="org"),
        ):
            verifier = CloudflareAccessVerifier(CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="https://access.example",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                jwt_verifier=PyJWTAccessJWTVerifier(
                    jwks_url="https://access.example/cdn-cgi/access/certs",
                    expected_issuer="https://access.example",
                    expected_audience="factory-console",
                    jwks_client=_JWKS(),
                    decoder=lambda *args, result=invalid, **kwargs: result,
                ),
            ))
            with self.assertRaises(AccessDenied):
                verifier.require_owner({"Cf-Access-Jwt-Assertion": "valid"})

    def test_pyjwt_verifier_normalizes_a_documented_access_audience_list(self) -> None:
        from factory_console.auth import PyJWTAccessJWTVerifier

        verifier = PyJWTAccessJWTVerifier(
            jwks_url="https://access.example/cdn-cgi/access/certs",
            expected_issuer="https://access.example",
            expected_audience="factory-console",
            jwks_client=_JWKS(),
            decoder=lambda *args, **kwargs: _claims(aud=["factory-console"]),
        )

        identity = verifier.verify("valid")

        self.assertEqual(identity.audience, "factory-console")

    def test_pyjwt_verifier_rejects_audience_list_without_the_configured_application(self) -> None:
        from factory_console.auth import AccessDenied, PyJWTAccessJWTVerifier

        verifier = PyJWTAccessJWTVerifier(
            jwks_url="https://access.example/cdn-cgi/access/certs",
            expected_issuer="https://access.example",
            expected_audience="factory-console",
            jwks_client=_JWKS(),
            decoder=lambda *args, **kwargs: _claims(aud=["another-application"]),
        )

        with self.assertRaises(AccessDenied):
            verifier.verify("valid")


class CSRFTokenTests(unittest.TestCase):
    def test_expired_signed_token_is_rejected(self) -> None:
        from factory_console.csrf import HMACCSRFTokenVerifier

        verifier = HMACCSRFTokenVerifier(b"a" * 32, ttl_seconds=60, now=lambda: 1_000)
        token = verifier.issue(owner_identity="owner-subject")

        expired = HMACCSRFTokenVerifier(b"a" * 32, ttl_seconds=60, now=lambda: 1_061)
        self.assertFalse(expired.verify(
            token=token,
            owner_identity="owner-subject",
            method="POST",
            path="/v1/jobs",
        ))

    def test_bootstrap_is_authenticated_and_only_issues_the_fixed_write_scope(self) -> None:
        from factory_console.api import CSRFConfig, CSRFProtection, FactoryConsoleWSGIApp
        from factory_console.auth import CloudflareAccessConfig, CloudflareAccessVerifier
        from factory_console.csrf import HMACCSRFTokenVerifier
        from factory_console.domain import ConsoleJobService

        app = FactoryConsoleWSGIApp(
            ConsoleJobService(_Factory()),
            CloudflareAccessVerifier(CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="https://access.example",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                jwt_verifier=_Verifier(),
            )),
            CSRFProtection(CSRFConfig(
                expected_origin="https://console.example",
                token_verifier=HMACCSRFTokenVerifier(b"a" * 32, ttl_seconds=60),
            )),
        )

        status: list[str] = []
        headers: list[tuple[str, str]] = []
        body = b"".join(app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/v1/csrf",
            "HTTP_CF_ACCESS_JWT_ASSERTION": "valid",
            "HTTP_ORIGIN": "https://console.example",
            "HTTP_X_FACTORY_CONSOLE_CSRF_BOOTSTRAP": "1",
        }, lambda value, response_headers: (status.append(value), headers.extend(response_headers))))

        self.assertEqual(status[0], "200 OK")
        self.assertEqual(json.loads(body)["scope"], "factory-review-writes")
        cookie = dict(headers)["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        token = cookie.split(";", 1)[0].split("=", 1)[1]

        status.clear()
        body = json.dumps({
            "company_name": "Example Hotel",
            "industry": "hospitality",
            "purpose": "answer booking questions",
            "primary_contact": "owner@example.com",
            "supported_languages": ["en"],
        }).encode("utf-8")
        response = b"".join(app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/jobs",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_CF_ACCESS_JWT_ASSERTION": "valid",
            "HTTP_ORIGIN": "https://console.example",
            "HTTP_X_FACTORY_CONSOLE_CSRF": token,
            "HTTP_COOKIE": f"factory_csrf={token}",
        }, lambda value, response_headers: status.append(value)))
        self.assertEqual(status[0], "201 Created")
        self.assertEqual(json.loads(response)["state"], "draft")

        status.clear()
        response = b"".join(app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/v1/csrf",
            "HTTP_ORIGIN": "https://console.example",
            "HTTP_X_FACTORY_CONSOLE_CSRF_BOOTSTRAP": "1",
        }, lambda value, response_headers: status.append(value)))
        self.assertEqual(status[0], "403 Forbidden")
        self.assertEqual(json.loads(response), {"error": "owner authorization required"})

        status.clear()
        body = b"".join(app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/v1/csrf/POST/v1/jobs",
            "HTTP_CF_ACCESS_JWT_ASSERTION": "valid",
            "HTTP_ORIGIN": "https://console.example",
        }, lambda value, response_headers: status.append(value)))
        self.assertEqual(status[0], "404 Not Found")
        self.assertEqual(json.loads(body), {"error": "not found"})


class RuntimeConfigurationTests(unittest.TestCase):
    def test_startup_refuses_missing_runtime_config(self) -> None:
        from factory_console.runtime import RuntimeConfigurationError, load_runtime_config

        with self.assertRaises(RuntimeConfigurationError):
            load_runtime_config("/definitely/missing/factory-console.json")

    def test_runtime_rejects_a_world_readable_csrf_secret(self) -> None:
        from factory_console.runtime import RuntimeConfigurationError, load_runtime_config

        with tempfile.TemporaryDirectory() as temporary:
            config_path = self._runtime_config(Path(temporary))
            with self.assertRaises(RuntimeConfigurationError):
                load_runtime_config(config_path, stat_for_path=self._stat_fixture(0o644, 4242), service_gid=4242)

    def test_runtime_accepts_a_root_owned_service_group_readable_csrf_secret(self) -> None:
        from factory_console.runtime import load_runtime_config

        with tempfile.TemporaryDirectory() as temporary:
            config_path = self._runtime_config(Path(temporary))
            config = load_runtime_config(config_path, stat_for_path=self._stat_fixture(0o640, 4242), service_gid=4242)

        self.assertEqual(config.csrf_secret_file.name, "csrf-signing.key")

    def test_runtime_rejects_a_root_only_csrf_secret_the_service_cannot_read(self) -> None:
        from factory_console.runtime import RuntimeConfigurationError, load_runtime_config

        with tempfile.TemporaryDirectory() as temporary:
            config_path = self._runtime_config(Path(temporary))
            with self.assertRaises(RuntimeConfigurationError):
                load_runtime_config(config_path, stat_for_path=self._stat_fixture(0o400, 0), service_gid=4242)

    @staticmethod
    def _runtime_config(root: Path) -> Path:
        config_path = root / "runtime.json"
        config_path.write_text(json.dumps({
            "version": 1,
            "policy_path": str(root / "policy.json"),
            "factory_config_path": str(root / "factory.json"),
            "csrf_secret_file": str(root / "csrf-signing.key"),
            "manifests": {"Example Hotel": str(root / "manifest.json")},
        }), encoding="utf-8")
        return config_path

    @staticmethod
    def _stat_fixture(secret_mode: int, service_gid: int):
        def stat_for_path(path: Path):
            mode = secret_mode if path.name == "csrf-signing.key" else 0o640
            return type("Metadata", (), {"st_mode": stat.S_IFREG | mode, "st_uid": 0, "st_gid": service_gid})()
        return stat_for_path


class JobLifecycleTests(unittest.TestCase):
    def test_factory_exposes_read_only_generation_state_for_console_adapters(self) -> None:
        from smartpbx_agent_factory.orchestrator import GenerationOrchestrator

        self.assertTrue(callable(getattr(GenerationOrchestrator, "generation_state", None)))

    def test_owner_can_advance_only_through_reviewed_lifecycle(self) -> None:
        from factory_console.domain import ConsoleJobService, IntakeValidationError, JobState

        factory = _Factory()
        service = ConsoleJobService(factory)
        job = service.create({
            "company_name": "Example Hotel",
            "industry": "hospitality",
            "purpose": "answer booking questions",
            "primary_contact": "owner@example.com",
            "supported_languages": ["en"],
        })

        self.assertEqual(job.state, JobState.DRAFT)
        job = service.inspect(job.job_id)
        self.assertEqual(job.state, JobState.INSPECTED)
        job = service.prepare_plan(job.job_id)
        self.assertEqual(job.state, JobState.KNOWLEDGE_REVIEW_REQUIRED)
        job = service.approve_knowledge(job.job_id, "c" * 64)
        self.assertEqual(job.state, JobState.PLAN_REVIEW_REQUIRED)
        job = service.approve_plan(job.job_id, "d" * 64)
        job = service.generate(job.job_id)
        job = service.verify(job.job_id)
        job = service.open_pr(job.job_id)

        self.assertEqual(job.state, JobState.PR_READY)
        self.assertEqual(factory.operations, ["inspect", "plan", "approve_knowledge", "approve_plan", "generate", "verify", "open_pr"])
        with self.assertRaises(IntakeValidationError):
            service.create({"company_name": "Example", "api_key": "not-allowed"})
        with self.assertRaises(IntakeValidationError):
            service.create({
                "company_name": "Example Hotel",
                "industry": "hospitality",
                "purpose": "load https://untrusted.example/manifest",
                "primary_contact": "owner@example.com",
                "supported_languages": ["en"],
            })

    def test_generation_is_rejected_until_owner_approval(self) -> None:
        from factory_console.domain import ConsoleJobService, InvalidStateTransition

        service = ConsoleJobService(_Factory())
        job = service.create({
            "company_name": "Example Hotel",
            "industry": "hospitality",
            "purpose": "answer booking questions",
            "primary_contact": "owner@example.com",
            "supported_languages": ["en"],
        })

        with self.assertRaises(InvalidStateTransition):
            service.generate(job.job_id)

    def test_plan_and_plan_approval_cannot_bypass_knowledge_confirmation(self) -> None:
        from factory_console.domain import ConsoleJobService, InvalidStateTransition

        service = ConsoleJobService(_Factory())
        job = service.create({
            "company_name": "Example Hotel",
            "industry": "hospitality",
            "purpose": "answer booking questions",
            "primary_contact": "owner@example.com",
            "supported_languages": ["en"],
        })
        service.inspect(job.job_id)
        job = service.prepare_plan(job.job_id)

        with self.assertRaises(InvalidStateTransition):
            service.generate(job.job_id)
        with self.assertRaises(InvalidStateTransition):
            service.approve_plan(job.job_id, "d" * 64)
        with self.assertRaises(InvalidStateTransition):
            service.approve_knowledge(job.job_id, "wrong")

    def test_factory_provenance_block_is_exposed_as_a_blocked_job(self) -> None:
        from factory_console.domain import ConsoleJobService, JobState

        service = ConsoleJobService(_BlockingFactory())
        job = service.create({
            "company_name": "Example Hotel",
            "industry": "hospitality",
            "purpose": "answer booking questions",
            "primary_contact": "owner@example.com",
            "supported_languages": ["en"],
        })
        service.inspect(job.job_id)

        job = service.prepare_plan(job.job_id)

        self.assertEqual(job.state, JobState.BLOCKED)


class HttpSafetyTests(unittest.TestCase):
    def test_no_deploy_route_or_operation_exists(self) -> None:
        from factory_console.api import CSRFConfig, CSRFProtection, FactoryConsoleWSGIApp
        from factory_console.auth import CloudflareAccessConfig, CloudflareAccessVerifier
        from factory_console.domain import ConsoleJobService, ConsoleOperation

        self.assertNotIn("deploy", {operation.value for operation in ConsoleOperation})
        app = FactoryConsoleWSGIApp(
            ConsoleJobService(_Factory()),
            CloudflareAccessVerifier(CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="https://access.example",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                jwt_verifier=_Verifier(),
            )),
            CSRFProtection(CSRFConfig(expected_origin="https://console.example", token_verifier=_CSRF())),
        )
        status: list[str] = []
        body = b"".join(app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/jobs/deploy",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(),
            "HTTP_CF_ACCESS_JWT_ASSERTION": "valid",
            "HTTP_ORIGIN": "https://console.example",
            "HTTP_X_FACTORY_CONSOLE_CSRF": "csrf-valid",
            "HTTP_COOKIE": "factory_csrf=csrf-valid",
        }, lambda value, headers: status.append(value)))

        self.assertEqual(status[0], "404 Not Found")
        self.assertEqual(json.loads(body), {"error": "not found"})

    def test_mutating_routes_require_origin_and_server_verified_csrf_token(self) -> None:
        from factory_console.api import CSRFConfig, CSRFProtection, FactoryConsoleWSGIApp
        from factory_console.auth import CloudflareAccessConfig, CloudflareAccessVerifier
        from factory_console.domain import ConsoleJobService

        app = FactoryConsoleWSGIApp(
            ConsoleJobService(_Factory()),
            CloudflareAccessVerifier(CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="https://access.example",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                jwt_verifier=_Verifier(),
            )),
            CSRFProtection(CSRFConfig(expected_origin="https://console.example", token_verifier=_CSRF())),
        )
        status: list[str] = []
        body = b"".join(app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/jobs",
            "CONTENT_LENGTH": "2",
            "wsgi.input": io.BytesIO(b"{}"),
            "HTTP_CF_ACCESS_JWT_ASSERTION": "valid",
            "HTTP_ORIGIN": "https://attacker.example",
            "HTTP_X_FACTORY_CONSOLE_CSRF": "csrf-valid",
        }, lambda value, headers: status.append(value)))

        self.assertEqual(status[0], "403 Forbidden")
        self.assertEqual(json.loads(body), {"error": "request origin or CSRF token rejected"})


class ReviewerRoleTests(unittest.TestCase):
    def test_reviewer_can_only_intake_inspect_and_plan(self) -> None:
        from factory_console.api import CSRFConfig, CSRFProtection, FactoryConsoleWSGIApp
        from factory_console.auth import CloudflareAccessConfig, CloudflareAccessVerifier, ReviewerIdentity
        from factory_console.domain import ConsoleJobService

        factory = _Factory()
        app = FactoryConsoleWSGIApp(
            ConsoleJobService(factory),
            CloudflareAccessVerifier(CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="https://access.example",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                reviewers=(ReviewerIdentity("reviewer-subject", "reviewer@example.com"),),
                jwt_verifier=_Verifier(),
            )),
            CSRFProtection(CSRFConfig(expected_origin="https://console.example", token_verifier=_ReviewerCSRF())),
        )

        def request(path: str, payload: dict[str, object]) -> tuple[str, dict[str, object]]:
            body = json.dumps(payload).encode("utf-8")
            status: list[str] = []
            response = b"".join(app({
                "REQUEST_METHOD": "POST",
                "PATH_INFO": path,
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
                "HTTP_CF_ACCESS_JWT_ASSERTION": "reviewer",
                "HTTP_ORIGIN": "https://console.example",
                "HTTP_X_FACTORY_CONSOLE_CSRF": "reviewer-csrf",
                "HTTP_COOKIE": "factory_csrf=reviewer-csrf",
            }, lambda value, headers: status.append(value)))
            return status[0], json.loads(response)

        status, created = request("/v1/jobs", {
            "company_name": "Review Hotel",
            "industry": "hospitality",
            "purpose": "review an intake",
            "primary_contact": "owner@example.com",
            "supported_languages": ["en"],
        })
        self.assertEqual(status, "201 Created")
        job_id = created["job_id"]
        self.assertEqual(request(f"/v1/jobs/{job_id}/inspect", {})[0], "200 OK")
        self.assertEqual(request(f"/v1/jobs/{job_id}/plan", {})[0], "200 OK")

        read_status: list[str] = []
        b"".join(app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": f"/v1/jobs/{job_id}",
            "HTTP_CF_ACCESS_JWT_ASSERTION": "reviewer",
        }, lambda value, headers: read_status.append(value)))
        self.assertEqual(read_status[0], "403 Forbidden")

        for operation in ("approve-knowledge", "approve-plan", "generate", "verify", "open-pr"):
            self.assertEqual(request(f"/v1/jobs/{job_id}/{operation}", {"digest": "c" * 64})[0], "403 Forbidden")
        self.assertEqual(factory.operations, ["inspect", "plan"])


class ReviewerBootstrapTests(unittest.TestCase):
    def test_reviewer_bootstraps_csrf_then_creates_test_intake(self) -> None:
        from factory_console.api import CSRFConfig, CSRFProtection, FactoryConsoleWSGIApp
        from factory_console.auth import CloudflareAccessConfig, CloudflareAccessVerifier, ReviewerIdentity
        from factory_console.csrf import HMACCSRFTokenVerifier
        from factory_console.domain import ConsoleJobService

        app = FactoryConsoleWSGIApp(
            ConsoleJobService(_Factory()),
            CloudflareAccessVerifier(CloudflareAccessConfig(
                expected_audience="factory-console",
                expected_issuer="https://access.example",
                owner_subject="owner-subject",
                owner_email="owner@example.com",
                reviewers=(ReviewerIdentity("reviewer-subject", "reviewer@example.com"),),
                jwt_verifier=_Verifier(),
            )),
            CSRFProtection(CSRFConfig(
                expected_origin="https://console.example",
                token_verifier=HMACCSRFTokenVerifier(b"a" * 32, ttl_seconds=60, now=lambda: 1_000),
            )),
        )

        bootstrap_status: list[str] = []
        bootstrap_headers: list[tuple[str, str]] = []
        bootstrap_body = b"".join(app({
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/v1/csrf",
            "HTTP_CF_ACCESS_JWT_ASSERTION": "reviewer",
            "HTTP_ORIGIN": "https://console.example",
            "HTTP_X_FACTORY_CONSOLE_CSRF_BOOTSTRAP": "1",
        }, lambda value, headers: (bootstrap_status.append(value), bootstrap_headers.extend(headers))))

        self.assertEqual(bootstrap_status[0], "200 OK")
        self.assertEqual(json.loads(bootstrap_body), {"scope": "factory-review-writes"})
        cookie = dict(bootstrap_headers)["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=300", cookie)
        token = cookie.split(";", 1)[0].split("=", 1)[1]

        intake = json.dumps({
            "company_name": "Review Hotel",
            "industry": "hospitality",
            "purpose": "review an intake",
            "primary_contact": "owner@example.com",
            "supported_languages": ["en"],
        }).encode("utf-8")
        create_status: list[str] = []
        create_body = b"".join(app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/jobs",
            "CONTENT_LENGTH": str(len(intake)),
            "wsgi.input": io.BytesIO(intake),
            "HTTP_CF_ACCESS_JWT_ASSERTION": "reviewer",
            "HTTP_ORIGIN": "https://console.example",
            "HTTP_X_FACTORY_CONSOLE_CSRF": token,
            "HTTP_COOKIE": f"factory_csrf={token}",
        }, lambda value, headers: create_status.append(value)))

        self.assertEqual(create_status[0], "201 Created")
        self.assertEqual(json.loads(create_body)["state"], "draft")
