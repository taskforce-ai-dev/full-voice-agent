"""Strict, synthetic-only Task 6 evidence contracts.

These tests exercise a local JSON fixture. They never read a dotenv file, log,
audio capture, call identifier, or provider credential.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "collect_sinhala_acceptance_evidence.py"
SOURCES = PROJECT_ROOT / "acceptance" / "task6-synthetic-failover-sources.json"
SCHEMA = PROJECT_ROOT / "acceptance" / "task6-pr-evidence.schema.json"


def load_collector():
    spec = importlib.util.spec_from_file_location("task6_evidence_collector", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def synthetic_sources() -> dict:
    return json.loads(SOURCES.read_text(encoding="utf-8"))


def collect_approved_artifact(collector, sources: dict) -> dict:
    return collector.collect_pr_artifact(
        sources,
        reviewed_pr_sha="a" * 40,
        pr_number=304,
        ci_run_id=123456,
        ci_run_attempt=1,
        operator_token="operator-review-304",
        senior_token="senior-review-304",
        operator_decision="approve",
        senior_decision="approve",
        senior_independent=True,
    )


def test_synthetic_fixture_normalizes_to_only_allowlisted_privacy_safe_fields():
    collector = load_collector()

    normalized = collector.normalize_sources(synthetic_sources())

    assert set(normalized) == {
        "synthetic_cases", "profile_snapshots", "turn_summaries", "overlap_summary",
    }
    assert {case["case_id"] for case in normalized["synthetic_cases"]} == collector.REQUIRED_CASE_IDS
    serialized = json.dumps(normalized, sort_keys=True)
    for forbidden in (
        "caller", "call_id", "audio", "prompt", "argument", "result_text",
        "api_key", "authorization", "exception", "payload", "transcript",
    ):
        assert forbidden not in serialized


def test_collector_rejects_unknown_or_textual_source_fields_before_artifact_output():
    collector = load_collector()
    sources = synthetic_sources()
    sources["synthetic_cases"][0]["raw_exception"] = "do not serialize this"

    with pytest.raises(collector.EvidenceValidationError, match="invalid_synthetic_case"):
        collector.normalize_sources(sources)


def test_pr_evidence_requires_explicit_identity_and_distinct_approving_reviewers():
    collector = load_collector()
    sources = synthetic_sources()

    with pytest.raises(collector.EvidenceValidationError, match="invalid_reviewed_pr_sha"):
        collector.collect_pr_artifact(
            sources,
            reviewed_pr_sha="not-a-sha",
            pr_number=304,
            ci_run_id=123456,
            ci_run_attempt=1,
            operator_token="operator-review-304",
            senior_token="senior-review-304",
            operator_decision="approve",
            senior_decision="approve",
            senior_independent=True,
        )

    with pytest.raises(collector.EvidenceValidationError, match="non_accepting_review"):
        collector.collect_pr_artifact(
            sources,
            reviewed_pr_sha="a" * 40,
            pr_number=304,
            ci_run_id=123456,
            ci_run_attempt=1,
            operator_token="same-review-token",
            senior_token="same-review-token",
            operator_decision="approve",
            senior_decision="approve",
            senior_independent=True,
        )


def test_pr_artifact_round_trips_without_release_or_runtime_identity_fields():
    collector = load_collector()
    artifact = collect_approved_artifact(collector, synthetic_sources())

    assert collector.validate_pr_artifact(copy.deepcopy(artifact)) == artifact
    serialized = json.dumps(artifact, sort_keys=True)
    for forbidden in ("release_sha", "digest", "caller", "audio", "transcript", "prompt", "api_key"):
        assert forbidden not in serialized


def test_pr_artifact_rejects_nonapproving_operator_or_extra_runtime_field():
    collector = load_collector()
    artifact = collect_approved_artifact(collector, synthetic_sources())
    artifact["operator_review"]["thresholds_ok"] = False

    with pytest.raises(collector.EvidenceValidationError, match="non_accepting_review"):
        collector.validate_pr_artifact(artifact)

    sources = synthetic_sources()
    sources["profile_snapshots"][0]["gemini_client"] = "forbidden"
    with pytest.raises(collector.EvidenceValidationError, match="invalid_profile_snapshot"):
        collector.normalize_sources(sources)


def test_versioned_schema_is_strict_and_pr_only():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert schema["additionalProperties"] is False
    assert schema["properties"]["phase"] == {"const": "pr"}
    assert schema["properties"]["identity"]["properties"]["reviewed_pr_sha"]["pattern"] == "^[0-9a-f]{40}$"
    assert schema["properties"]["identity"]["properties"].keys() == {
        "reviewed_pr_sha", "pr_number", "ci_run_id", "ci_run_attempt",
    }
