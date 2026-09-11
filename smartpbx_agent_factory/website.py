"""Render non-secret, backend-gated website demo cards."""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .gitops import WorktreeHandle, WorktreeManager, WorktreeConflictError, manager_owned_worktree_target
from .model import AgentManifest
from .resources import DerivedResources


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")
_LANGUAGES = frozenset(("en", "ar", "ru", "si"))
_ENDPOINT = re.compile(r"(?:wss://|smartpbx-[a-z0-9-]+\.taskforceai\.tech)", re.I)
_PRIVATE_MARKER = r"-----BEGIN " + r"(?:[A-Z ]+ )?" + r"PRIVATE " + r"KEY-----"
_CREDENTIAL = re.compile(
    rf"(?:{_PRIVATE_MARKER}|eyJ[A-Za-z0-9_-]{{6,}}\.[A-Za-z0-9_-]{{6,}}\.[A-Za-z0-9_-]{{3,}}|"
    r"(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_-]{20,}|"
    r"gh[pous]_[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"(?:api[_ -]?key|token|secret|password|credential)\s*[:=]\s*\S+|"
    r"authorization\s*:\s*bearer\s+\S+|bearer\s+[A-Za-z0-9._~-]{8,})",
    re.I,
)
_RESERVED_FIELD = re.compile(r"(?:token|secret|credential|password|api[_-]?key|backendhost|demohost|wss|host|url)", re.I)
_TOKEN_URL = "https://hattonhills.taskforceai.tech/api/voice-token"
_IMPORT = "import { SMARTPBX_AGENT_CARDS } from '../../data/smartpbx-agents.generated';\n"
_ACTIVE_FILTER = 'SMARTPBX_AGENT_CARDS.filter((card) => card.releaseState === "active")'
_MARKER_START = "/* smartpbx-agent-factory-data-v1\n"
_MARKER_END = "\n*/"
_TRANSACTION_MARKER = ".smartpbx-agent-factory-website-transaction.json"
_TRANSACTION_ROOT = ".smartpbx-agent-factory-website-txn"
_TRANSACTION_VERSION = 4
_TRANSACTION_TARGETS = (
    "data/smartpbx-agents.generated.mjs",
    "scripts/validate-smartpbx-card.mjs",
    "components/pages/BookDemo.tsx",
    "package.json",
)
_CARD_KEYS = {
    "id", "releaseState", "brand", "agentName", "role", "location", "description",
    "images", "trainedOn", "langs", "callLabel", "askHint", "steps",
}


@dataclass(frozen=True)
class WebsiteDependency:
    backend_artifact_digest: str
    backend_branch_sha: str
    release_order: tuple[str, str] = ("backend", "website")


@dataclass(frozen=True)
class WebsiteRenderReport:
    dependency: WebsiteDependency
    card_ids: tuple[str, ...]
    output_paths: tuple[Path, ...]


def _dependency(digest: str, sha: str) -> WebsiteDependency:
    if not _DIGEST.fullmatch(digest):
        raise ValueError("backend_artifact_digest must be a lowercase SHA-256 digest")
    if not _SHA.fullmatch(sha):
        raise ValueError("backend_branch_sha must be a full lowercase Git SHA")
    return WebsiteDependency(digest, sha)


def _public(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"website card field must not be empty: {field}")
    if _ENDPOINT.search(value):
        raise ValueError(f"website card field contains a non-public endpoint: {field}")
    if _CREDENTIAL.search(value):
        raise ValueError(f"website card field contains a credential-like value: {field}")
    return value.strip()


def _public_tree(value: object, field: str) -> None:
    if isinstance(value, str):
        _public(value, field)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _public_tree(item, f"{field}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            if _RESERVED_FIELD.search(str(key)):
                raise ValueError(f"website card field is not public: {key}")
            _public_tree(item, f"{field}.{key}")


def _card(manifest: AgentManifest, resources: DerivedResources) -> dict[str, object]:
    if resources.slug != manifest.slug:
        raise ValueError("website resources must belong to the manifest slug")
    if not manifest.website_demo.enabled:
        raise ValueError("website demo is disabled")
    if manifest.website_demo.visibility != "pending":
        raise ValueError("website generation only permits pending cards")
    langs_raw = manifest.website_demo.supported_languages or tuple(item.code for item in manifest.languages)
    langs = tuple(dict.fromkeys(_public(item, "langs").split("-", 1)[0].lower() for item in langs_raw))
    if not langs or any(item not in _LANGUAGES for item in langs):
        raise ValueError("website demo languages must be supported public language values")
    brand = _public(manifest.public_name, "brand")
    purpose = _public(manifest.purpose.rstrip("."), "description")
    card = {
        "id": _public(manifest.slug, "id"),
        "releaseState": "pending",
        "brand": brand,
        "agentName": _public(manifest.agent_name, "agentName"),
        "role": "Information Assistant",
        "location": "Online",
        "description": purpose + ".",
        "images": [],
        "trainedOn": ["approved company information"],
        "langs": list(langs),
        "callLabel": "Call " + brand,
        "askHint": _public(", ".join(manifest.allowed_topics) or "approved company information", "askHint"),
        "steps": [{"bold": "Ask a question.", "rest": "Use the approved company information."}],
    }
    _public_tree(card, "card")
    return card


def _validate_card(card: object) -> dict[str, object]:
    if not isinstance(card, dict) or set(card) != _CARD_KEYS:
        raise ValueError("existing generated website card has an invalid public shape")
    if not isinstance(card["id"], str) or not _SLUG.fullmatch(card["id"]):
        raise ValueError("existing generated website card has an invalid id")
    if card["releaseState"] not in {"pending", "active"}:
        raise ValueError("existing generated website card has an invalid releaseState")
    if not isinstance(card["langs"], list) or not card["langs"] or any(item not in _LANGUAGES for item in card["langs"]):
        raise ValueError("existing generated website card has unsupported languages")
    _public_tree(card, "card")
    return card


def _validate_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict) or set(record) != {"id", "digest", "sha"}:
        raise ValueError("existing generated website dependency has an invalid shape")
    if not isinstance(record["id"], str) or not _SLUG.fullmatch(record["id"]):
        raise ValueError("existing generated website dependency has an invalid id")
    _dependency(record["digest"], record["sha"])
    return record


def _read_data(path: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not path.exists():
        return [], []
    source = path.read_text(encoding="utf-8")
    if not source.startswith(_MARKER_START):
        raise ValueError("existing generated website data is not factory-owned")
    end = source.find(_MARKER_END, len(_MARKER_START))
    if end < 0:
        raise ValueError("existing generated website data marker is incomplete")
    try:
        payload = json.loads(source[len(_MARKER_START):end])
    except json.JSONDecodeError as exc:
        raise ValueError("existing generated website data marker is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"cards", "dependencies"}:
        raise ValueError("existing generated website data marker has an invalid shape")
    cards = [_validate_card(item) for item in payload["cards"]] if isinstance(payload["cards"], list) else None
    records = [_validate_record(item) for item in payload["dependencies"]] if isinstance(payload["dependencies"], list) else None
    if cards is None or records is None:
        raise ValueError("existing generated website data marker has invalid collections")
    card_ids, record_ids = [item["id"] for item in cards], [item["id"] for item in records]
    if len(card_ids) != len(set(card_ids)) or len(record_ids) != len(set(record_ids)):
        raise ValueError("existing generated website data contains duplicate ids")
    if set(card_ids) != set(record_ids):
        raise ValueError("existing generated website data has unmatched card dependencies")
    return cards, records


def _merge(cards: list[dict[str, object]], records: list[dict[str, object]], card: dict[str, object], dependency: WebsiteDependency) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    cards_by_id = {str(item["id"]): item for item in cards}
    records_by_id = {str(item["id"]): item for item in records}
    identifier = str(card["id"])
    prior = records_by_id.get(identifier)
    if prior is not None and (prior["digest"], prior["sha"]) != (dependency.backend_artifact_digest, dependency.backend_branch_sha):
        raise ValueError("conflicting generated website card dependency for existing id")
    if identifier in cards_by_id and prior is None:
        raise ValueError("conflicting generated website card lacks an owned dependency")
    cards_by_id[identifier] = card
    records_by_id[identifier] = {"id": identifier, "digest": dependency.backend_artifact_digest, "sha": dependency.backend_branch_sha}
    ids = sorted(cards_by_id)
    return [cards_by_id[item] for item in ids], [records_by_id[item] for item in ids]


def _literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ": "))


def _safe_module(module: str) -> None:
    if _TOKEN_URL in module:
        raise ValueError("generated website data must not contain the shared token URL")
    if _ENDPOINT.search(module) or _CREDENTIAL.search(module):
        raise ValueError("generated website data contains a credential-like value or endpoint")
    if any(item in module for item in ("tokenUrl", "backendHost", "DEMO_AGENT_HOSTS")):
        raise ValueError("generated website data contains a prohibited routing field")


def _module(cards: list[dict[str, object]], records: list[dict[str, object]]) -> str:
    marker = json.dumps({"cards": cards, "dependencies": records}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    lines = [_MARKER_START.rstrip("\n"), marker, "*/", "// Generated public review metadata only.", "", "export const SMARTPBX_AGENT_CARDS = Object.freeze(["]
    for card in cards:
        lines.append("  {")
        for key in ("id", "releaseState", "brand", "agentName", "role", "location", "description", "images", "trainedOn", "langs", "callLabel", "askHint", "steps"):
            lines.append(f"    {key}: {_literal(card[key])},")
        lines.append("  },")
    lines.extend(("]);", "", "export const SMARTPBX_AGENT_DEPENDENCIES = Object.freeze(["))
    for record in records:
        lines.extend(("  {", f"    id: {_literal(record['id'])},", f"    backendArtifactDigest: {_literal(record['digest'])},", f"    backendBranchSha: {_literal(record['sha'])},", "    releaseOrder: Object.freeze([\"backend\", \"website\"]),", "  },"))
    lines.extend(("]);", ""))
    result = "\n".join(lines)
    _safe_module(result)
    return result


def _book_demo(source: str) -> str:
    if _TOKEN_URL not in source:
        raise ValueError("BookDemo.tsx does not contain the shared HattonHills token issuer")
    if "encodeURIComponent(agent.id)" not in source:
        raise ValueError("BookDemo.tsx does not pass the selected agent to the shared issuer")
    if "params: { agent: agent.id, lang }" not in source:
        raise ValueError("BookDemo.tsx does not preserve agent and language connection parameters")
    if _IMPORT not in source:
        anchor = "import { contactPageSchema } from '../../lib/schema';\n"
        if anchor not in source:
            raise ValueError("BookDemo.tsx import seam is unavailable")
        source = source.replace(anchor, anchor + _IMPORT, 1)
    if _ACTIVE_FILTER not in source:
        if "const AGENTS: Agent[] = [" not in source:
            raise ValueError("BookDemo.tsx agent list seam is unavailable")
        ending = "\n];\n\nconst langMeta"
        if ending not in source:
            raise ValueError("BookDemo.tsx agent list closing seam is unavailable")
        source = source.replace("const AGENTS: Agent[] = [", "const STATIC_AGENTS: Agent[] = [", 1)
        source = source.replace(ending, "\n];\n\nconst AGENTS: Agent[] = [\n  ...STATIC_AGENTS,\n  ..." + _ACTIVE_FILTER + ",\n];\n\nconst langMeta", 1)
    return source


def _validator() -> str:
    return """import { readFileSync } from "node:fs";
import { SMARTPBX_AGENT_CARDS } from "../data/smartpbx-agents.generated.mjs";
const SUPPORTED = new Set(["en", "ar", "ru", "si"]);
const RESERVED = /(?:token|secret|credential|password|api[_-]?key|backendhost|demohost|wss|host|url)/i;
const UNSAFE = /(?:wss:\\/\\/|smartpbx-[a-z0-9-]+\\.taskforceai\\.tech|eyJ[A-Za-z0-9_-]{6,}\\.[A-Za-z0-9_-]{6,}\\.[A-Za-z0-9_-]{3,}|(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|(?:api[_ -]?key|token|secret|password|credential)\\s*[:=]\\s*\\S+)/i;
function check(ok, message) { if (!ok) throw new Error("smartpbx card contract: " + message); }
function publicValue(value, path) {
  if (typeof value === "string") { check(!UNSAFE.test(value), path + " is not public"); return; }
  if (Array.isArray(value)) { value.forEach((item, index) => publicValue(item, path + "[" + index + "]")); return; }
  if (value && typeof value === "object") Object.entries(value).forEach(([key, item]) => { check(!RESERVED.test(key), path + "." + key + " is not public"); publicValue(item, path + "." + key); });
}
export function callableCards(cards) { return cards.filter((card) => card.releaseState === "active"); }
export function validateCards(cards) {
  check(Array.isArray(cards), "cards must be an array");
  cards.forEach((card, index) => { const path = "cards[" + index + "]"; check(/^[a-z][a-z0-9-]*$/.test(card.id), path + ".id is invalid"); check(["pending", "active"].includes(card.releaseState), path + ".releaseState is invalid"); check(Array.isArray(card.langs) && card.langs.length && card.langs.every((lang) => SUPPORTED.has(lang)), path + ".langs is invalid"); publicValue(card, path); });
  return cards;
}
const shared = { brand: "Fixture", agentName: "Ava", role: "Information Assistant", location: "Online", description: "Answers approved questions.", images: [], trainedOn: [], langs: ["en"], callLabel: "Call Fixture", askHint: "approved questions", steps: [] };
const fixture = [{ ...shared, id: "pending-agent", releaseState: "pending" }, { ...shared, id: "active-agent", releaseState: "active" }];
validateCards(fixture); check(callableCards(fixture).map((card) => card.id).join(",") === "active-agent", "only active cards are callable");
validateCards(SMARTPBX_AGENT_CARDS);
check(readFileSync(new URL("../components/pages/BookDemo.tsx", import.meta.url), "utf8").includes('SMARTPBX_AGENT_CARDS.filter((card) => card.releaseState === "active")'), "BookDemo must filter active cards");
"""


def _package(source: str) -> str:
    value = json.loads(source)
    scripts = value.get("scripts")
    command = "node scripts/validate-smartpbx-card.mjs"
    if not isinstance(scripts, dict):
        raise ValueError("website package.json must contain a scripts object")
    if scripts.get("test:smartpbx") not in {None, command}:
        raise ValueError("website package.json already has an incompatible test:smartpbx command")
    if scripts.get("test:smartpbx") == command:
        return source
    scripts["test:smartpbx"] = command
    return json.dumps(value, ensure_ascii=True, indent=2) + "\n"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _require_owned_output_root(worktree: WorktreeHandle, manager: WorktreeManager) -> Path:
    try:
        output_dir = manager_owned_worktree_target(manager, worktree)
    except WorktreeConflictError as exc:
        raise ValueError(str(exc)) from exc
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError("manager-owned website worktree target must be a real non-symlink directory")
    for parent in (output_dir, *output_dir.parents):
        if parent.is_symlink():
            raise ValueError("manager-owned website worktree target must not be beneath a symlink")
    return output_dir.resolve(strict=True)


def _owned_path(output_dir: Path, relative: str) -> Path:
    candidate = output_dir / relative
    try:
        candidate.relative_to(output_dir)
    except ValueError as exc:
        raise ValueError("owned path is outside output_dir") from exc
    current = output_dir
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("owned path must not be a symlink")
        if current.exists() and not current.resolve(strict=True).is_relative_to(output_dir):
            raise ValueError("owned path escapes output_dir")
    if not candidate.resolve(strict=False).is_relative_to(output_dir):
        raise ValueError("owned path escapes output_dir")
    return candidate


def _transaction_paths(output_dir: Path) -> tuple[Path, Path]:
    return _owned_path(output_dir, _TRANSACTION_MARKER), _owned_path(output_dir, _TRANSACTION_ROOT)


def _atomic_bytes(path: Path, value: bytes, *, scope: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + scope + ".", suffix=".tmp", dir=path.parent)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_name, path)
    _fsync_directory(path.parent)


def _durable_backup(path: Path, value: bytes) -> tuple[int, str]:
    with path.open("wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)
    return len(value), hashlib.sha256(value).hexdigest()


def _load_transaction(output_dir: Path) -> tuple[Path, Path, list[dict[str, object]], list[str], tuple[str, ...], str] | None:
    marker_path, transaction_root = _transaction_paths(output_dir)
    if marker_path.is_symlink() or transaction_root.is_symlink():
        raise ValueError("transaction marker paths must not be symlinks")
    if not marker_path.exists():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("transaction marker is unreadable") from exc
    if not isinstance(marker, dict):
        raise ValueError("transaction marker has an invalid shape")
    version = marker.get("version")
    if version == 1:
        required = {"version", "stage", "files", "created_dirs"}
        applied: list[object] = list(_TRANSACTION_TARGETS)
        phase = "active"
    elif version == _TRANSACTION_VERSION:
        required = {"version", "stage", "files", "applied", "created_dirs", "phase"}
        applied = marker.get("applied")
        phase = marker.get("phase")
    elif version == 3:
        required = {"version", "stage", "files", "applied", "created_dirs", "phase"}
        applied = marker.get("applied")
        phase = marker.get("phase")
    elif version == 2:
        required = {"version", "stage", "files", "applied", "created_dirs"}
        applied = marker.get("applied")
        phase = "active"
    else:
        raise ValueError("transaction marker has an invalid shape")
    if set(marker) != required:
        raise ValueError("transaction marker has an invalid shape")
    stage = marker["stage"]
    files = marker["files"]
    created_dirs = marker.get("created_dirs", [])
    if not isinstance(stage, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", stage):
        raise ValueError("transaction marker has an unsafe stage")
    if not isinstance(files, list) or not isinstance(applied, list) or not isinstance(created_dirs, list) or phase not in {"preparing", "active", "cleanup"}:
        raise ValueError("transaction marker has invalid collections")
    if {item.get("target") for item in files if isinstance(item, dict)} != set(_TRANSACTION_TARGETS):
        raise ValueError("transaction marker has unsafe targets")
    if len(files) != len(_TRANSACTION_TARGETS):
        raise ValueError("transaction marker has duplicate targets")
    if (
        not all(isinstance(target, str) for target in applied)
        or len(applied) != len(set(applied))
        or any(target not in _TRANSACTION_TARGETS for target in applied)
        or applied != [target for target in _TRANSACTION_TARGETS if target in applied]
        or (phase == "preparing" and applied)
    ):
        raise ValueError("transaction marker has unsafe applied targets")
    if any(item not in {"data", "scripts"} for item in created_dirs):
        raise ValueError("transaction marker has unsafe created directories")
    stage_path = _owned_path(output_dir, f"{_TRANSACTION_ROOT}/{stage}")
    if phase == "active":
        if stage_path.is_symlink() or not stage_path.is_dir():
            raise ValueError("transaction marker staging directory is unavailable")
        if {item.name for item in transaction_root.iterdir()} != {stage}:
            raise ValueError("transaction marker staging root has unexpected contents")
    elif transaction_root.exists():
        if transaction_root.is_symlink() or not transaction_root.is_dir():
            raise ValueError("transaction marker staging root is unavailable")
        if stage_path.exists():
            if stage_path.is_symlink() or not stage_path.is_dir() or {item.name for item in transaction_root.iterdir()} != {stage}:
                raise ValueError("transaction marker staging root has unexpected contents")
        elif any(transaction_root.iterdir()):
            raise ValueError("transaction marker staging root has unexpected contents")
    validated: list[dict[str, object]] = []
    expected_backups: set[str] = set()
    for index, record in enumerate(files):
        if not isinstance(record, dict) or set(record) != {"target", "existed", "backup", "size", "sha256"}:
            raise ValueError("transaction marker file record is invalid")
        existed, backup, size, digest = record["existed"], record["backup"], record["size"], record["sha256"]
        if not isinstance(existed, bool):
            raise ValueError("transaction marker file record has invalid existence")
        expected_backup = f"backup-{index}.bin"
        if existed and (backup != expected_backup or not isinstance(size, int) or size < 0 or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise ValueError("transaction marker file record has an unsafe backup")
        if not existed and (backup is not None or size is not None or digest is not None):
            raise ValueError("transaction marker file record has an unexpected backup")
        if existed:
            expected_backups.add(expected_backup)
        if existed and stage_path.exists() and phase == "active":
            backup_path = _owned_path(output_dir, f"{_TRANSACTION_ROOT}/{stage}/{expected_backup}")
            if backup_path.is_symlink() or not backup_path.is_file():
                raise ValueError("transaction marker backup must not be a symlink")
            payload = backup_path.read_bytes()
            if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("transaction marker backup integrity check failed")
        elif existed and stage_path.exists():
            backup_path = _owned_path(output_dir, f"{_TRANSACTION_ROOT}/{stage}/{expected_backup}")
            if backup_path.is_symlink() or (backup_path.exists() and not backup_path.is_file()):
                raise ValueError("transaction marker backup must not be a symlink")
        validated.append(record)
    if stage_path.exists():
        contents = {item.name for item in stage_path.iterdir()}
        if (phase == "active" and contents != expected_backups) or (phase in {"preparing", "cleanup"} and not contents.issubset(expected_backups)):
            raise ValueError("transaction marker staging directory has unexpected contents")
    return marker_path, stage_path, validated, created_dirs, tuple(applied), phase


def _write_transaction_marker(
    marker_path: Path,
    *,
    stage: str,
    files: list[dict[str, object]],
    created_dirs: list[str],
    applied: tuple[str, ...],
    phase: str,
) -> None:
    if phase not in {"preparing", "active", "cleanup"}:
        raise ValueError("transaction marker phase is invalid")
    marker = {
        "version": _TRANSACTION_VERSION,
        "stage": stage,
        "files": files,
        "applied": list(applied),
        "created_dirs": created_dirs,
        "phase": phase,
    }
    _atomic_bytes(marker_path, json.dumps(marker, sort_keys=True).encode("utf-8"), scope="smartpbx-transaction")


def _record_transaction_progress(
    output_dir: Path,
    *,
    target: str,
    applied: bool,
) -> None:
    """Durably record a target that may have been replaced before mutating it."""
    if target not in _TRANSACTION_TARGETS:
        raise ValueError("transaction target is not factory-owned")
    transaction = _load_transaction(output_dir)
    if transaction is None:
        raise ValueError("transaction marker is unavailable")
    marker_path, stage_path, files, created_dirs, recorded, phase = transaction
    if phase != "active":
        raise ValueError("transaction is already in terminal cleanup")
    values = set(recorded)
    if applied:
        values.add(target)
    else:
        values.discard(target)
    _write_transaction_marker(
        marker_path,
        stage=stage_path.name,
        files=files,
        created_dirs=created_dirs,
        applied=tuple(item for item in _TRANSACTION_TARGETS if item in values),
        phase="active",
    )


def _recover_transaction(output_dir: Path) -> None:
    transaction = _load_transaction(output_dir)
    if transaction is None:
        return
    _, _, files, _, applied, phase = transaction
    if phase in {"preparing", "cleanup"}:
        _finish_transaction(output_dir)
        return
    for index, record in enumerate(files):
        if record["target"] not in applied:
            continue
        transaction = _load_transaction(output_dir)
        if transaction is None:
            raise ValueError("transaction marker is unavailable")
        _, stage_path, _, _, _, current_phase = transaction
        if current_phase != "active":
            raise ValueError("transaction entered cleanup during recovery")
        target = _owned_path(output_dir, str(record["target"]))
        if record["existed"]:
            backup_path = _owned_path(output_dir, f"{_TRANSACTION_ROOT}/{stage_path.name}/backup-{index}.bin")
            if backup_path.is_symlink():
                raise ValueError("transaction marker backup must not be a symlink")
            payload = backup_path.read_bytes()
            if len(payload) != record["size"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
                raise ValueError("transaction marker backup integrity check failed")
            _atomic_bytes(target, payload, scope="smartpbx-recover")
        else:
            target.unlink(missing_ok=True)

    _finish_transaction(output_dir)


def _cleanup_terminal_transaction(output_dir: Path, stage_path: Path, files: list[dict[str, object]]) -> None:
    """Remove only marker-owned staging contents before retiring its cleanup marker."""
    transaction_root = stage_path.parent
    if stage_path.exists():
        if stage_path.is_symlink() or not stage_path.is_dir():
            raise ValueError("transaction marker staging directory is unavailable")
        for index, record in enumerate(files):
            if record["existed"]:
                backup = stage_path / f"backup-{index}.bin"
                if backup.is_symlink() or (backup.exists() and not backup.is_file()):
                    raise ValueError("transaction marker backup must not be a symlink")
                if backup.exists():
                    backup.unlink()
        stage_path.rmdir()
        _fsync_directory(transaction_root)
    if transaction_root.exists():
        if transaction_root.is_symlink() or not transaction_root.is_dir() or any(transaction_root.iterdir()):
            raise ValueError("transaction marker staging root has unexpected contents")
        transaction_root.rmdir()
        _fsync_directory(output_dir)


def _finish_transaction(output_dir: Path) -> None:
    transaction = _load_transaction(output_dir)
    if transaction is None:
        return
    marker_path, stage_path, files, created_dirs, applied, phase = transaction
    if phase in {"preparing", "active"}:
        _write_transaction_marker(
            marker_path,
            stage=stage_path.name,
            files=files,
            created_dirs=created_dirs,
            applied=applied,
            phase="cleanup",
        )
        transaction = _load_transaction(output_dir)
        if transaction is None:
            raise ValueError("transaction marker is unavailable")
        marker_path, stage_path, files, created_dirs, _, phase = transaction
    if phase != "cleanup":
        raise ValueError("transaction marker cleanup phase is invalid")
    _cleanup_terminal_transaction(output_dir, stage_path, files)
    marker_path.unlink()
    _fsync_directory(output_dir)
    for relative in reversed(created_dirs):
        try:
            (output_dir / relative).rmdir()
        except OSError:
            pass


def _begin_transaction(output_dir: Path, originals: Mapping[Path, bytes | None], created_dirs: list[str], *, scope: str) -> None:
    marker_path, transaction_root = _transaction_paths(output_dir)
    if transaction_root.exists() and (transaction_root.is_symlink() or not transaction_root.is_dir() or any(transaction_root.iterdir())):
        raise ValueError("transaction staging root must be an empty real directory")
    files: list[dict[str, object]] = []
    for index, relative in enumerate(_TRANSACTION_TARGETS):
        original = originals[output_dir / relative]
        backup, size, digest = None, None, None
        if original is not None:
            backup = f"backup-{index}.bin"
            size, digest = len(original), hashlib.sha256(original).hexdigest()
        files.append({"target": relative, "existed": original is not None, "backup": backup, "size": size, "sha256": digest})
    stage = scope + "." + os.urandom(16).hex()
    _write_transaction_marker(
        marker_path,
        stage=stage,
        files=files,
        created_dirs=created_dirs,
        applied=(),
        phase="preparing",
    )
    transaction_root.mkdir(exist_ok=True)
    _fsync_directory(output_dir)
    stage_path = _owned_path(output_dir, f"{_TRANSACTION_ROOT}/{stage}")
    stage_path.mkdir()
    _fsync_directory(transaction_root)
    for index, record in enumerate(files):
        if record["existed"]:
            original = originals[output_dir / str(record["target"])]
            if original is None:
                raise ValueError("transaction backup source is unavailable")
            _durable_backup(stage_path / f"backup-{index}.bin", original)
    _write_transaction_marker(
        marker_path,
        stage=stage,
        files=files,
        created_dirs=created_dirs,
        applied=(),
        phase="active",
    )


def _atomic(output_dir: Path, contents: Mapping[Path, bytes], scope: str) -> None:
    for relative in _TRANSACTION_TARGETS:
        _owned_path(output_dir, relative)
    originals = {path: path.read_bytes() if path.exists() else None for path in contents}
    changed = {path: value for path, value in contents.items() if originals[path] != value}
    if not changed:
        return
    created_dirs = [relative for relative in ("data", "scripts") if not (output_dir / relative).exists()]
    _begin_transaction(output_dir, originals, created_dirs, scope=scope)
    staged: dict[Path, Path] = {}
    pending_target: str | None = None
    try:
        for path, value in changed.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix="." + scope + ".", suffix=".tmp", dir=path.parent)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            staged[path] = Path(temporary_name)
        for path, temporary in staged.items():
            relative = path.relative_to(output_dir).as_posix()
            _record_transaction_progress(output_dir, target=relative, applied=True)
            pending_target = relative
            try:
                os.replace(temporary, path)
            except Exception:
                # A raised replacement did not mutate this POSIX destination, so
                # recovery must not retry the same failing replacement for it.
                _record_transaction_progress(output_dir, target=relative, applied=False)
                pending_target = None
                raise
            pending_target = None
            _fsync_directory(path.parent)
    except Exception:
        if pending_target is not None:
            _record_transaction_progress(output_dir, target=pending_target, applied=False)
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        _recover_transaction(output_dir)
        raise
    _finish_transaction(output_dir)


def render_website_artifacts(
    manifest: AgentManifest,
    resources: DerivedResources,
    *,
    backend_artifact_digest: str,
    backend_branch_sha: str,
    worktree: WorktreeHandle,
    worktree_manager: WorktreeManager,
) -> WebsiteRenderReport:
    """Write review-only artifacts to an isolated website worktree."""
    dependency = _dependency(backend_artifact_digest, backend_branch_sha)
    output_dir = _require_owned_output_root(worktree, worktree_manager)
    _recover_transaction(output_dir)
    page, package = _owned_path(output_dir, "components/pages/BookDemo.tsx"), _owned_path(output_dir, "package.json")
    data, validator = _owned_path(output_dir, "data/smartpbx-agents.generated.mjs"), _owned_path(output_dir, "scripts/validate-smartpbx-card.mjs")
    if not page.is_file() or not package.is_file():
        raise ValueError("output_dir must be an isolated website worktree with BookDemo.tsx and package.json")
    card = _card(manifest, resources)
    cards, records = _merge(*_read_data(data), card, dependency)
    contents = {
        data: _module(cards, records).encode(),
        validator: _validator().encode(),
        page: _book_demo(page.read_text(encoding="utf-8")).encode(),
        package: _package(package.read_text(encoding="utf-8")).encode(),
    }
    _atomic(output_dir, contents, "smartpbx-" + manifest.slug + "-" + backend_artifact_digest[:12])
    return WebsiteRenderReport(dependency, tuple(str(item["id"]) for item in cards), (Path("data/smartpbx-agents.generated.mjs"), Path("scripts/validate-smartpbx-card.mjs"), Path("components/pages/BookDemo.tsx"), Path("package.json")))
