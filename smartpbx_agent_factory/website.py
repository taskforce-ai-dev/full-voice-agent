"""Render non-secret, backend-gated website demo cards."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
    r"(?:api[_ -]?key|token|secret|password|credential)\s*[:=]\s*\S+)",
    re.I,
)
_RESERVED_FIELD = re.compile(r"(?:token|secret|credential|password|api[_-]?key|backendhost|demohost|wss|host|url)", re.I)
_TOKEN_URL = "https://hattonhills.taskforceai.tech/api/voice-token"
_IMPORT = "import { SMARTPBX_AGENT_CARDS } from '../../data/smartpbx-agents.generated';\n"
_ACTIVE_FILTER = 'SMARTPBX_AGENT_CARDS.filter((card) => card.releaseState === "active")'
_MARKER_START = "/* smartpbx-agent-factory-data-v1\n"
_MARKER_END = "\n*/"
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


def _atomic(contents: Mapping[Path, bytes], scope: str) -> None:
    originals = {path: path.read_bytes() if path.exists() else None for path in contents}
    changed = {path: value for path, value in contents.items() if originals[path] != value}
    if not changed:
        return
    created, staged, replaced = [], {}, []
    try:
        for path, value in changed.items():
            if not path.parent.exists():
                path.parent.mkdir(parents=True)
                created.append(path.parent)
            fd, name = tempfile.mkstemp(prefix="." + scope + ".", suffix=".tmp", dir=path.parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
            staged[path] = Path(name)
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
    except Exception:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        for path in reversed(replaced):
            if originals[path] is None:
                path.unlink(missing_ok=True)
            else:
                fd, name = tempfile.mkstemp(prefix="." + scope + ".restore.", suffix=".tmp", dir=path.parent)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(originals[path])
                os.replace(name, path)
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                pass
        raise


def render_website_artifacts(manifest: AgentManifest, resources: DerivedResources, *, backend_artifact_digest: str, backend_branch_sha: str, output_dir: Path) -> WebsiteRenderReport:
    """Write review-only artifacts to an isolated website worktree."""
    dependency = _dependency(backend_artifact_digest, backend_branch_sha)
    output_dir = Path(output_dir)
    page, package = output_dir / "components/pages/BookDemo.tsx", output_dir / "package.json"
    data, validator = output_dir / "data/smartpbx-agents.generated.mjs", output_dir / "scripts/validate-smartpbx-card.mjs"
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
    _atomic(contents, "smartpbx-" + manifest.slug + "-" + backend_artifact_digest[:12])
    return WebsiteRenderReport(dependency, tuple(str(item["id"]) for item in cards), (Path("data/smartpbx-agents.generated.mjs"), Path("scripts/validate-smartpbx-card.mjs"), Path("components/pages/BookDemo.tsx"), Path("package.json")))
