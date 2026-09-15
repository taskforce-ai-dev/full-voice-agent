"""Executable security and lifecycle contracts for the factory console."""

from __future__ import annotations

import io
import json
import unittest
from dataclasses import dataclass


@dataclass(frozen=True)
class _Identity:
    subject: str
    audience: str


class _Verifier:
    def verify(self, token: str) -> _Identity:
        if token == "valid":
            return _Identity(subject="owner@example.com", audience="factory-console")
        return _Identity(subject="other@example.com", audience="factory-console")


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
            stage=Stage.PLAN_REVIEW_REQUIRED,
            plan_digest="c" * 64,
        )

    def approve_plan(self, job, generation):
        self.operations.append("approve_plan")
        generation.approve_plan(generation.plan_digest or "")
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


class CloudflareAccessTests(unittest.TestCase):
    def test_rejects_missing_or_non_owner_identity(self) -> None:
        from factory_console.auth import AccessDenied, CloudflareAccessConfig, CloudflareAccessVerifier

        verifier = CloudflareAccessVerifier(
            CloudflareAccessConfig(
                expected_audience="factory-console",
                owner_identity="owner@example.com",
                jwt_verifier=_Verifier(),
            )
        )

        with self.assertRaises(AccessDenied):
            verifier.require_owner({})
        with self.assertRaises(AccessDenied):
            verifier.require_owner({"Authorization": "Bearer not-owner"})

    def test_accepts_a_verified_cloudflare_access_assertion(self) -> None:
        from factory_console.auth import CloudflareAccessConfig, CloudflareAccessVerifier

        verifier = CloudflareAccessVerifier(CloudflareAccessConfig(
            expected_audience="factory-console",
            owner_identity="owner@example.com",
            jwt_verifier=_Verifier(),
        ))

        identity = verifier.require_owner({"Cf-Access-Jwt-Assertion": "valid"})

        self.assertEqual(identity.subject, "owner@example.com")

    def test_production_configuration_fails_closed_without_jwt_verifier(self) -> None:
        from factory_console.auth import AccessConfigurationError, CloudflareAccessConfig

        with self.assertRaises(AccessConfigurationError):
            CloudflareAccessConfig(
                expected_audience="factory-console",
                owner_identity="owner@example.com",
                jwt_verifier=None,
            ).validate_for_production()


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
        self.assertEqual(job.state, JobState.PLAN_REVIEW_REQUIRED)
        job = service.approve_for_generation(job.job_id)
        job = service.generate(job.job_id)
        job = service.verify(job.job_id)
        job = service.open_pr(job.job_id)

        self.assertEqual(job.state, JobState.PR_READY)
        self.assertEqual(factory.operations, ["inspect", "plan", "approve_plan", "generate", "verify", "open_pr"])
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
        from factory_console.api import FactoryConsoleWSGIApp
        from factory_console.auth import CloudflareAccessConfig, CloudflareAccessVerifier
        from factory_console.domain import ConsoleJobService, ConsoleOperation

        self.assertNotIn("deploy", {operation.value for operation in ConsoleOperation})
        app = FactoryConsoleWSGIApp(
            ConsoleJobService(_Factory()),
            CloudflareAccessVerifier(CloudflareAccessConfig(
                expected_audience="factory-console",
                owner_identity="owner@example.com",
                jwt_verifier=_Verifier(),
            )),
        )
        status: list[str] = []
        body = b"".join(app({
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/jobs/deploy",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(),
            "HTTP_AUTHORIZATION": "Bearer valid",
        }, lambda value, headers: status.append(value)))

        self.assertEqual(status[0], "404 Not Found")
        self.assertEqual(json.loads(body), {"error": "not found"})
