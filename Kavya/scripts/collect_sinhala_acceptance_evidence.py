#!/usr/bin/env python3
"""Build strict, privacy-safe Task 6 PR evidence from synthetic observations.

This program is intentionally not a production telemetry collector.  It only
normalizes allowlisted, test-provided observations.  A PR acceptance artifact
requires an explicit immutable PR identity and two separate opaque review
tokens; CI validates the source fixture but cannot manufacture either review.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Final


SCHEMA_VERSION: Final = "task6-pr-evidence/v1"
SOURCE_SCHEMA_VERSION: Final = "task6-synthetic-source/v1"
SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CASE_ID_RE: Final = re.compile(r"^[a-z0-9_]{1,64}$")
PROVIDERS: Final = frozenset({"claude", "gemini", "openai"})
MODELS: Final = frozenset({
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-5",
    "gemini-2.5-flash",
    "gemini-3.7-flash",
    "gpt-4o",
})
OUTCOMES: Final = frozenset({
    "completed",
    "true_empty",
    "incomplete_tool_block",
    "malformed_tool_json",
    "stream_aborted",
    "timeout",
    "closed_failure",
})
RECOVERY_REASONS: Final = frozenset({
    "none", "quota", "server", "client", "empty", "malformed",
    "cancelled", "deadline", "tool_executed",
})
REQUIRED_CASE_IDS: Final = frozenset({
    "provider_quota",
    "provider_server",
    "provider_client",
    "injected_client_without_global_key",
    "fallback_ready",
    "fallback_unavailable",
    "retry_state_reset",
    "partial_output_cancelled",
    "tool_side_effect_no_replay",
    "malformed_or_empty_payload",
    "terminal_metadata",
    "concurrent_ownership_restored",
})
LATENCY_KEYS: Final = frozenset({
    "latency_ms", "latency_upper_bound_ms", "upper_bound_is_conservative",
})


class EvidenceValidationError(ValueError):
    """Validation failure with a fixed safe diagnostic code."""


def reject(code: str) -> None:
    raise EvidenceValidationError(code)


def require_exact_keys(value: Any, expected: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        reject(code)
    return value


def require_nonnegative_int(value: Any, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        reject(code)
    return value


def require_case_id(value: Any) -> str:
    if not isinstance(value, str) or not CASE_ID_RE.fullmatch(value):
        reject("invalid_case_id")
    return value


def require_enum(value: Any, allowed: frozenset[str], code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        reject(code)
    return value


def normalize_latency(record: dict[str, Any]) -> dict[str, Any]:
    keys = set(record) & LATENCY_KEYS
    if keys == {"latency_ms"}:
        return {"latency_ms": require_nonnegative_int(record["latency_ms"], 600000, "invalid_latency")}
    if keys == {"latency_upper_bound_ms", "upper_bound_is_conservative"}:
        if record["upper_bound_is_conservative"] is not True:
            reject("invalid_latency")
        return {
            "latency_upper_bound_ms": require_nonnegative_int(
                record["latency_upper_bound_ms"], 600000, "invalid_latency"
            ),
            "upper_bound_is_conservative": True,
        }
    reject("invalid_latency")


def normalize_synthetic_case(record: Any) -> tuple[int, dict[str, Any]]:
    required = {
        "observation_index", "case_id", "result", "initial_provider", "final_provider",
        "recovery_reason", "llm_request_count", "tool_call_count", "history_committed",
        "cleanup_owner_matched",
    }
    source = require_exact_keys(record, required | (set(record) & LATENCY_KEYS), "invalid_synthetic_case")
    index = require_nonnegative_int(source["observation_index"], 1_000_000, "invalid_observation_index")
    if source["result"] not in {"pass", "fail"}:
        reject("invalid_result")
    if not isinstance(source["history_committed"], bool) or not isinstance(source["cleanup_owner_matched"], bool):
        reject("invalid_boolean")
    normalized = {
        "case_id": require_case_id(source["case_id"]),
        "result": source["result"],
        "initial_provider": require_enum(source["initial_provider"], PROVIDERS, "unsupported_provider_or_model"),
        "final_provider": require_enum(source["final_provider"], PROVIDERS, "unsupported_provider_or_model"),
        "recovery_reason": require_enum(source["recovery_reason"], RECOVERY_REASONS, "invalid_recovery_reason"),
        "llm_request_count": require_nonnegative_int(source["llm_request_count"], 100, "invalid_counter"),
        "tool_call_count": require_nonnegative_int(source["tool_call_count"], 100, "invalid_counter"),
        "history_committed": source["history_committed"],
        "cleanup_owner_matched": source["cleanup_owner_matched"],
        "latency": normalize_latency(source),
    }
    return index, normalized


def normalize_profile_snapshot(record: Any) -> tuple[int, dict[str, Any]]:
    source = require_exact_keys(
        record,
        {"observation_index", "case_id", "press", "language", "provider", "model", "stt_locale", "tts_route"},
        "invalid_profile_snapshot",
    )
    index = require_nonnegative_int(source["observation_index"], 1_000_000, "invalid_observation_index")
    if source["press"] not in {1, 2} or source["language"] not in {"en", "si"}:
        reject("invalid_profile_snapshot")
    provider = require_enum(source["provider"], PROVIDERS, "unsupported_provider_or_model")
    model = require_enum(source["model"], MODELS, "unsupported_provider_or_model")
    if ((provider == "claude" and not model.startswith("claude-"))
            or (provider == "gemini" and not model.startswith("gemini-"))
            or (provider == "openai" and model != "gpt-4o")):
        reject("unsupported_provider_or_model")
    if source["stt_locale"] not in {"en-US", "si-LK"} or source["tts_route"] not in {"elevenlabs", "gemini"}:
        reject("invalid_profile_snapshot")
    if source["press"] == 1 and (
        source["language"] != "en" or source["stt_locale"] != "en-US" or source["tts_route"] != "elevenlabs"
    ):
        reject("invalid_profile_snapshot")
    if source["press"] == 2 and (
        source["language"] != "si" or source["stt_locale"] != "si-LK" or source["tts_route"] != "gemini"
    ):
        reject("invalid_profile_snapshot")
    return index, {
        "case_id": require_case_id(source["case_id"]),
        "press": source["press"],
        "language": source["language"],
        "provider": provider,
        "model": model,
        "stt_locale": source["stt_locale"],
        "tts_route": source["tts_route"],
    }


def normalize_turn_summary(record: Any) -> tuple[int, dict[str, Any]]:
    required = {
        "observation_index", "case_id", "llm_request_count", "tool_call_count",
        "history_committed", "cleanup_owner_matched", "outcome",
    }
    source = require_exact_keys(record, required | (set(record) & LATENCY_KEYS), "invalid_turn_summary")
    index = require_nonnegative_int(source["observation_index"], 1_000_000, "invalid_observation_index")
    if not isinstance(source["history_committed"], bool) or not isinstance(source["cleanup_owner_matched"], bool):
        reject("invalid_boolean")
    return index, {
        "case_id": require_case_id(source["case_id"]),
        "llm_request_count": require_nonnegative_int(source["llm_request_count"], 100, "invalid_counter"),
        "tool_call_count": require_nonnegative_int(source["tool_call_count"], 100, "invalid_counter"),
        "history_committed": source["history_committed"],
        "cleanup_owner_matched": source["cleanup_owner_matched"],
        "outcome": require_enum(source["outcome"], OUTCOMES, "invalid_outcome"),
        "latency": normalize_latency(source),
    }


def normalize_overlap_summary(record: Any) -> tuple[int, dict[str, bool]]:
    fields = {
        "press1_profile_ok", "press2_profile_ok", "distinct_call_local_profiles",
        "cross_call_ownership_ok", "cross_call_tool_state_ok", "cross_call_cleanup_ok",
    }
    source = require_exact_keys(record, fields | {"observation_index"}, "invalid_overlap_summary")
    index = require_nonnegative_int(source["observation_index"], 1_000_000, "invalid_observation_index")
    if any(source[field] is not True for field in fields):
        reject("failed_overlap_summary")
    return index, {field: True for field in sorted(fields)}


def normalize_sources(payload: Any) -> dict[str, Any]:
    source = require_exact_keys(
        payload,
        {"schema_version", "source_kind", "synthetic_cases", "profile_snapshots", "turn_summaries", "overlap_summaries"},
        "invalid_source_envelope",
    )
    if source["schema_version"] != SOURCE_SCHEMA_VERSION or source["source_kind"] != "synthetic":
        reject("invalid_source_envelope")
    for key in ("synthetic_cases", "profile_snapshots", "turn_summaries", "overlap_summaries"):
        if not isinstance(source[key], list):
            reject("invalid_source_envelope")

    synthetic = [normalize_synthetic_case(item) for item in source["synthetic_cases"]]
    profiles = [normalize_profile_snapshot(item) for item in source["profile_snapshots"]]
    turns = [normalize_turn_summary(item) for item in source["turn_summaries"]]
    overlaps = [normalize_overlap_summary(item) for item in source["overlap_summaries"]]
    if any(len(collection) != len({index for index, _ in collection})
           for collection in (synthetic, profiles, turns, overlaps)):
        reject("duplicate_observation_index")
    if {item["case_id"] for _, item in synthetic} != REQUIRED_CASE_IDS:
        reject("missing_required_synthetic_case")
    if any(item["result"] != "pass" for _, item in synthetic):
        reject("synthetic_case_failed")
    if len(profiles) != 2 or len(turns) != 2 or len(overlaps) != 1:
        reject("invalid_synthetic_cardinality")
    # A synthetic profile and its turn summary deliberately share an internal
    # observation index. The index never leaves this process or the artifact.
    if {index for index, _ in profiles} != {index for index, _ in turns}:
        reject("unmatched_observation_index")
    if {item["press"] for _, item in profiles} != {1, 2}:
        reject("invalid_synthetic_profile_set")
    profiles_by_index = {index: item for index, item in profiles}
    turns_by_index = {index: item for index, item in turns}
    if any(profiles_by_index[index]["case_id"] != turns_by_index[index]["case_id"] for index in profiles_by_index):
        reject("unmatched_observation_index")
    return {
        "synthetic_cases": [item for _, item in sorted(synthetic)],
        "profile_snapshots": [item for _, item in sorted(profiles)],
        "turn_summaries": [item for _, item in sorted(turns)],
        "overlap_summary": overlaps[0][1],
    }


def require_review_token(value: Any, code: str) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        reject(code)
    return value


def collect_pr_artifact(
    sources: dict[str, Any], *, reviewed_pr_sha: str, pr_number: int,
    ci_run_id: int, ci_run_attempt: int, operator_token: str, senior_token: str,
    operator_decision: str, senior_decision: str, senior_independent: bool,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(reviewed_pr_sha):
        reject("invalid_reviewed_pr_sha")
    require_nonnegative_int(pr_number, 2_147_483_647, "invalid_pr_number")
    if pr_number < 1:
        reject("invalid_pr_number")
    if not 1 <= ci_run_id <= 1_000_000_000_000 or not 1 <= ci_run_attempt <= 1000:
        reject("invalid_ci_identity")
    operator_token = require_review_token(operator_token, "invalid_operator_review")
    senior_token = require_review_token(senior_token, "invalid_senior_review")
    if operator_token == senior_token or operator_decision != "approve" or senior_decision != "approve" or senior_independent is not True:
        reject("non_accepting_review")
    observations = normalize_sources(sources)
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": "pr",
        "identity": {
            "reviewed_pr_sha": reviewed_pr_sha,
            "pr_number": pr_number,
            "ci_run_id": ci_run_id,
            "ci_run_attempt": ci_run_attempt,
        },
        "observations": observations,
        "operator_review": {
            "fluent_and_intelligible": True,
            "thresholds_ok": True,
            "decision": "approve",
            "reviewer_token": operator_token,
        },
        "senior_review": {
            "independent": True,
            "decision": "approve",
            "reviewer_token": senior_token,
        },
    }


def validate_pr_artifact(payload: Any) -> dict[str, Any]:
    artifact = require_exact_keys(
        payload,
        {"schema_version", "phase", "identity", "observations", "operator_review", "senior_review"},
        "invalid_artifact_envelope",
    )
    if artifact["schema_version"] != SCHEMA_VERSION or artifact["phase"] != "pr":
        reject("invalid_artifact_envelope")
    identity = require_exact_keys(
        artifact["identity"], {"reviewed_pr_sha", "pr_number", "ci_run_id", "ci_run_attempt"}, "invalid_identity"
    )
    observations = require_exact_keys(
        artifact["observations"],
        {"synthetic_cases", "profile_snapshots", "turn_summaries", "overlap_summary"},
        "invalid_observations",
    )
    operator_review = require_exact_keys(
        artifact["operator_review"],
        {"fluent_and_intelligible", "thresholds_ok", "decision", "reviewer_token"},
        "invalid_operator_review",
    )
    senior_review = require_exact_keys(
        artifact["senior_review"],
        {"independent", "decision", "reviewer_token"},
        "invalid_senior_review",
    )
    if operator_review["fluent_and_intelligible"] is not True or operator_review["thresholds_ok"] is not True:
        reject("non_accepting_review")

    def source_case(index: int, item: Any) -> dict[str, Any]:
        case = require_exact_keys(
            item,
            {
                "case_id", "result", "initial_provider", "final_provider", "recovery_reason",
                "llm_request_count", "tool_call_count", "history_committed",
                "cleanup_owner_matched", "latency",
            },
            "invalid_synthetic_case",
        )
        if not isinstance(case["latency"], dict):
            reject("invalid_latency")
        latency = require_exact_keys(
            case["latency"], set(case["latency"]) & LATENCY_KEYS, "invalid_latency"
        )
        return {
            "observation_index": index,
            **{key: value for key, value in case.items() if key != "latency"},
            **latency,
        }

    def source_turn(index: int, item: Any) -> dict[str, Any]:
        summary = require_exact_keys(
            item,
            {
                "case_id", "llm_request_count", "tool_call_count", "history_committed",
                "cleanup_owner_matched", "outcome", "latency",
            },
            "invalid_turn_summary",
        )
        if not isinstance(summary["latency"], dict):
            reject("invalid_latency")
        latency = require_exact_keys(
            summary["latency"], set(summary["latency"]) & LATENCY_KEYS, "invalid_latency"
        )
        return {
            "observation_index": index,
            **{key: value for key, value in summary.items() if key != "latency"},
            **latency,
        }

    def source_profile(index: int, item: Any) -> dict[str, Any]:
        snapshot = require_exact_keys(
            item,
            {"case_id", "press", "language", "provider", "model", "stt_locale", "tts_route"},
            "invalid_profile_snapshot",
        )
        return {"observation_index": index, **snapshot}

    return collect_pr_artifact(
        {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "source_kind": "synthetic",
            "synthetic_cases": [
                source_case(index + 1, case)
                for index, case in enumerate(observations["synthetic_cases"])
            ],
            "profile_snapshots": [
                source_profile(index + 101, item)
                for index, item in enumerate(observations["profile_snapshots"])
            ],
            "turn_summaries": [
                source_turn(index + 101, item)
                for index, item in enumerate(observations["turn_summaries"])
            ],
            "overlap_summaries": [{"observation_index": 301, **observations["overlap_summary"]}],
        },
        reviewed_pr_sha=identity["reviewed_pr_sha"],
        pr_number=identity["pr_number"],
        ci_run_id=identity["ci_run_id"],
        ci_run_attempt=identity["ci_run_attempt"],
        operator_token=operator_review["reviewer_token"],
        senior_token=senior_review["reviewer_token"],
        operator_decision=operator_review["decision"],
        senior_decision=senior_review["decision"],
        senior_independent=senior_review["independent"],
    )


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        reject("invalid_json_input")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def source_manifest(sources: dict[str, Any]) -> dict[str, Any]:
    observations = normalize_sources(sources)
    return {
        "schema_version": "task6-synthetic-manifest/v1",
        "source_kind": "synthetic",
        "checks": {
            "source_schema_valid": True,
            "required_synthetic_cases_present": True,
            "profile_snapshot_count_ok": len(observations["profile_snapshots"]) == 2,
            "turn_summary_count_ok": len(observations["turn_summaries"]) == 2,
            "overlap_summary_ok": observations["overlap_summary"] == {
                "cross_call_cleanup_ok": True,
                "cross_call_ownership_ok": True,
                "cross_call_tool_state_ok": True,
                "distinct_call_local_profiles": True,
                "press1_profile_ok": True,
                "press2_profile_ok": True,
            },
        },
        "synthetic_case_count": len(observations["synthetic_cases"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-synthetic")
    validate.add_argument("--sources", required=True, type=Path)
    validate.add_argument("--manifest", required=True, type=Path)
    collect = commands.add_parser("collect-pr")
    collect.add_argument("--sources", required=True, type=Path)
    collect.add_argument("--reviewed-pr-sha", required=True)
    collect.add_argument("--pr-number", required=True, type=int)
    collect.add_argument("--ci-run-id", required=True, type=int)
    collect.add_argument("--ci-run-attempt", required=True, type=int)
    collect.add_argument("--operator-review-token", required=True)
    collect.add_argument("--senior-review-token", required=True)
    collect.add_argument("--operator-decision", required=True, choices=("approve", "rollback", "escalate"))
    collect.add_argument("--senior-decision", required=True, choices=("approve", "rollback", "escalate"))
    collect.add_argument("--senior-independent", action="store_true")
    collect.add_argument("--output", required=True, type=Path)
    artifact = commands.add_parser("validate-pr")
    artifact.add_argument("--artifact", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-synthetic":
            write_json(args.manifest, source_manifest(read_json(args.sources)))
        elif args.command == "collect-pr":
            artifact = collect_pr_artifact(
                read_json(args.sources),
                reviewed_pr_sha=args.reviewed_pr_sha,
                pr_number=args.pr_number,
                ci_run_id=args.ci_run_id,
                ci_run_attempt=args.ci_run_attempt,
                operator_token=args.operator_review_token,
                senior_token=args.senior_review_token,
                operator_decision=args.operator_decision,
                senior_decision=args.senior_decision,
                senior_independent=args.senior_independent,
            )
            write_json(args.output, artifact)
        else:
            validate_pr_artifact(read_json(args.artifact))
    except EvidenceValidationError as exc:
        print(f"evidence_rejected reason={exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
