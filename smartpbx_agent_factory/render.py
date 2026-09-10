"""Fail-closed rendering for isolated, inquiry-only SmartPBX backend trees.

This module deliberately has no fallback to a mutable source tree.  The default
allowlist is digest-bound to an approved v06 source revision; a later protocol
overlay must be separately approved rather than relabeling that deployed source.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .gitops import WorktreeHandle, WorktreeManager, WorktreeConflictError, manager_owned_worktree_target
from .knowledge import KnowledgeError, KnowledgeReview, recompute_knowledge_review_digest
from .model import AgentManifest
from .provenance import ProvenanceError, TemplateAllowlist, render_template_text, validate_allowlist_metadata
from .resources import DerivedResources
from .schema import manifest_digest
from .state import GenerationState, Stage


class RenderError(ValueError):
    """Raised when a generated backend artifact would be unsafe or incomplete."""


class TemplateUnavailableError(RenderError):
    """Raised when no approved, immutable template allowlist is available."""


class IncompleteTemplateError(RenderError):
    """Raised when provenance exists but no complete client-neutral runtime exists."""


class IdentityLeakError(RenderError):
    """Raised when an output would contain another customer's identity or a secret."""


class ReviewNotApprovedError(RenderError):
    """Raised when knowledge review has not been approved for this render."""


@dataclass(frozen=True)
class RenderReport:
    output_dir: Path
    artifact_digest: str
    files: tuple[str, ...]
    enabled_capabilities: tuple[str, ...]
    template_version: str
    review_digest: str
    synthetic: bool
    deployable: bool
    runtime_status: str


_TEMPLATE_ROOT = Path(__file__).parent / "template_v1"
_DEFAULT_ALLOWLIST = _TEMPLATE_ROOT / "file_allowlist.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"hatton\s+hills", r"treehouse", r"mosvold", r"yanolja", r"kavya")
)
_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"begin\s+private\s+key",
        r"smartpbx_ws_token\s*=",
        r"(?:api[-_ ]?key)\s*[:=]\s*[^\s${]",
        r"\bsk-[a-z0-9_-]{12,}",
        r"\bAC[a-f0-9]{32}\b",
    )
)
_BUSINESS_TOOLS = ("create_booking", "transfer_to_human", "hangup_call")
_PROVIDER_RUNTIME = {
    "azure": {"requirements": ("azure-cognitiveservices-speech==1.51.1",), "environment": ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")},
    "claude": {"requirements": ("anthropic==0.120.2",), "environment": ("ANTHROPIC_API_KEY",)},
    "deepgram": {"requirements": ("httpx==0.28.1",), "environment": ("DEEPGRAM_API_KEY",)},
    "elevenlabs": {"requirements": ("httpx==0.28.1",), "environment": ("ELEVENLABS_API_KEY",)},
    "gemini": {"requirements": ("google-genai==2.16.0",), "environment": ("GEMINI_API_KEY",)},
    "google": {"requirements": ("google-cloud-speech==2.40.0",), "environment": ("GOOGLE_APPLICATION_CREDENTIALS",)},
    "rime": {"requirements": ("httpx==0.28.1",), "environment": ("RIME_API_KEY",)},
}


def _provider_runtime_contract(manifest: AgentManifest) -> tuple[dict[str, object], tuple[str, ...], str, str]:
    """Derive only the dependencies, secrets, and mounts selected by language profiles."""
    selected: set[str] = set()
    languages: dict[str, dict[str, str]] = {}
    for language in manifest.languages:
        lanes = {"stt": language.stt, "llm": language.llm, "tts": language.tts}
        unknown = sorted(set(lanes.values()) - set(_PROVIDER_RUNTIME))
        if unknown:
            raise RenderError(f"provider runtime is not approved: {unknown[0]}")
        selected.update(lanes.values())
        languages[language.code] = {
            "stt": language.stt, "stt_model": language.stt_model,
            "llm": language.llm, "llm_model": language.llm_model,
            "tts": language.tts, "tts_model": language.tts_model,
        }
    requirements = sorted({item for provider in selected for item in _PROVIDER_RUNTIME[provider]["requirements"]})
    environment = sorted({item for provider in selected for item in _PROVIDER_RUNTIME[provider]["environment"]})
    environment_lines = []
    for name in environment:
        if name == "GOOGLE_APPLICATION_CREDENTIALS":
            environment_lines.append(f'      {name}: "/app/gcp-credentials.json"')
        else:
            environment_lines.append(f'      {name}: "${{{name}:-}}"')
    volume_lines = ["      - ./gcp-credentials.json:/app/gcp-credentials.json:ro"] if "GOOGLE_APPLICATION_CREDENTIALS" in environment else []
    profile: dict[str, object] = {"languages": languages, "required_environment": environment}
    return profile, tuple(requirements), "\n".join(environment_lines), "\n".join(volume_lines)


def _load_default_allowlist() -> TemplateAllowlist:
    try:
        raw = json.loads(_DEFAULT_ALLOWLIST.read_text(encoding="utf-8"))
        if raw.get("status") == "partial":
            raise IncompleteTemplateError(
                "INCOMPLETE_TEMPLATE: verified v06 provenance has no complete client-neutral runtime extraction"
            )
        return validate_allowlist_metadata(raw)
    except IncompleteTemplateError:
        raise
    except (OSError, json.JSONDecodeError, ProvenanceError) as exc:
        raise TemplateUnavailableError(
            "TEMPLATE_ALLOWLIST_UNAVAILABLE: approved deployed template provenance is required"
        ) from exc


def _verify_supplied_templates(root: Path, allowlist: TemplateAllowlist) -> Mapping[str, str]:
    """Verify an already-approved allowlist without accepting unlisted files."""
    if not root.is_dir() or root.is_symlink():
        raise TemplateUnavailableError("TEMPLATE_ALLOWLIST_UNAVAILABLE: template root is not a safe directory")
    expected = {entry.template_path: entry.sha256 for entry in allowlist.files.values()}
    if not expected:
        raise TemplateUnavailableError("TEMPLATE_ALLOWLIST_UNAVAILABLE: template allowlist has no files")
    actual: dict[str, Path] = {}
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise TemplateUnavailableError(f"TEMPLATE_ALLOWLIST_UNAVAILABLE: template symlink: {relative}")
        if candidate.is_file():
            actual[relative] = candidate
    if set(actual) != set(expected):
        raise TemplateUnavailableError("TEMPLATE_ALLOWLIST_UNAVAILABLE: template files do not exactly match allowlist")
    verified: dict[str, str] = {}
    for relative, candidate in actual.items():
        digest = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != expected[relative]:
            raise TemplateUnavailableError(f"TEMPLATE_ALLOWLIST_UNAVAILABLE: template hash drift: {relative}")
        try:
            verified[relative] = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise TemplateUnavailableError(f"TEMPLATE_ALLOWLIST_UNAVAILABLE: template is not UTF-8: {relative}") from exc
    return verified


def _review_facts(review: KnowledgeReview, state: GenerationState | None, manifest: AgentManifest) -> tuple[str, ...]:
    if type(review) is not KnowledgeReview:
        raise ReviewNotApprovedError("knowledge review must be a concrete immutable KnowledgeReview dataclass")
    if not isinstance(state, GenerationState):
        raise ReviewNotApprovedError("knowledge review requires GenerationState approval")
    if state.manifest_digest != manifest_digest(manifest):
        raise ReviewNotApprovedError("generation state manifest digest does not match manifest")
    if state.stage not in {Stage.GENERATED, Stage.VERIFIED, Stage.THREE_PRS_OPENED}:
        raise ReviewNotApprovedError("generation state requires matching plan approval before rendering")
    if not state.plan_digest or state.plan_approval_digest != state.plan_digest:
        raise ReviewNotApprovedError("generation state requires matching plan approval before rendering")
    if not isinstance(review.digest, str) or not _SHA256_RE.fullmatch(review.digest):
        raise ReviewNotApprovedError("knowledge review digest must be a sha256 hex digest")
    if review.executed_instructions is not False:
        raise ReviewNotApprovedError("knowledge review must not contain executed instructions")
    if (
        state.knowledge_review_digest != review.digest
        or state.knowledge_approval_digest != review.digest
        or state.stage not in {Stage.PLAN_REVIEW_REQUIRED, Stage.GENERATED, Stage.VERIFIED, Stage.THREE_PRS_OPENED}
    ):
        raise ReviewNotApprovedError("knowledge review digest is not approved by GenerationState")
    try:
        expected_digest = recompute_knowledge_review_digest(review)
    except KnowledgeError as exc:
        raise ReviewNotApprovedError("knowledge review content is not canonical") from exc
    if review.digest != expected_digest:
        raise ReviewNotApprovedError("knowledge review canonical digest does not match reviewed content")
    return tuple(fact.text for fact in review.facts)


def _knowledge_documents(review: KnowledgeReview, facts: tuple[str, ...]) -> Mapping[str, str]:
    """Render source documents only under renderer-owned names after review validation."""
    if not review.documents:
        return {"approved-facts.md": "\n\n".join(facts) or "No approved facts were supplied."}
    return {
        f"source-{index:03d}.md": document.text
        for index, document in enumerate(review.documents, start=1)
    }


def _product_profile_payload(manifest: AgentManifest, documents: Mapping[str, str]) -> dict[str, object]:
    """Serialize only reviewed manifest and renderer-owned knowledge metadata.

    This stays separate from the call path: render-time data becomes one
    immutable JSON file, and the runtime startup seam loads it before the first
    session is admitted. Provider credentials are represented only by named
    identifiers in the reviewed catalogue, never by this product data.
    """
    languages: dict[str, dict[str, object]] = {}
    for language in manifest.languages:
        languages[language.code] = {
            "locale": language.locale,
            "stt": language.stt,
            "stt_model": language.stt_model or None,
            "llm": language.llm,
            "llm_model": language.llm_model or None,
            "tts": language.tts,
            "tts_model": language.tts_model or None,
            "fallback": language.fallback,
            "fallback_model": language.fallback_model or None,
            "greeting": language.greeting,
            "prompt_block": _language_prompt_block(manifest, language.code),
        }
    return {
        "identity": {
            "display_name": manifest.display_name,
            "public_name": manifest.public_name,
            "agent_name": manifest.agent_name,
            "industry": manifest.industry,
            "purpose": manifest.purpose,
            "audience": manifest.audience,
        },
        "languages": languages,
        "default_language": manifest.languages[0].code,
        "allowed_topics": list(manifest.allowed_topics),
        "refused_topics": list(manifest.refused_topics),
        "knowledge_paths": [f"knowledge_docs/{filename}" for filename in documents],
    }


def _language_prompt_block(manifest: AgentManifest, language_code: str) -> str:
    allowed = "; ".join(manifest.allowed_topics) or "approved information"
    refused = "; ".join(manifest.refused_topics) or "unapproved requests"
    return (
        f"You are {manifest.agent_name} for {manifest.public_name}. "
        f"Respond in {language_code}. Discuss only: {allowed}. "
        f"Refuse: {refused}."
    )


def _python_gateway(resources: DerivedResources) -> str:
    return f'''"""Generated SmartPBX gateway. Authentication always precedes accept()."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class SmartPBXSettings:
    token: str
    account_id: str
    auth_header_name: str = "{resources.wss_header}"
    max_calls: int = 1

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "SmartPBXSettings":
        token = environ.get("SMARTPBX_WS_TOKEN", "")
        account_id = environ.get("SMARTPBX_ACCOUNT_ID", "")
        header = environ.get("SMARTPBX_AUTH_HEADER_NAME", "{resources.wss_header}")
        if not token or not account_id or not header:
            raise ValueError("SmartPBX settings are incomplete")
        return cls(token=token, account_id=account_id, auth_header_name=header)

    def token_matches(self, candidate: object) -> bool:
        return isinstance(candidate, str) and candidate.isascii() and secrets.compare_digest(self.token, candidate)


class SmartPBXSessionRegistry:
    def __init__(self, max_sessions: int) -> None:
        if not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self.max_sessions = max_sessions


class SmartPBXGateway:
    def __init__(self, settings: SmartPBXSettings, registry: SmartPBXSessionRegistry) -> None:
        self.settings = settings
        self.registry = registry

    async def handle(self, websocket, factory) -> None:
        candidate = websocket.headers.get(self.settings.auth_header_name)
        if not self.settings.token_matches(candidate):
            await websocket.close(code=1008, reason="unauthorized")
            return
        await websocket.accept()
        session = None
        while True:
            event = json.loads(await websocket.receive_text())
            if event.get("event") == "start":
                start = event.get("start", {{}})
                if start.get("accountId") != self.settings.account_id:
                    await websocket.close(code=1008, reason="unauthorized")
                    return
                session = await factory(start, None)
                if hasattr(session, "start"):
                    await session.start()
            elif event.get("event") == "stop":
                if session is not None and hasattr(session, "finish"):
                    await session.finish()
                return
'''


def _files(
    manifest: AgentManifest, resources: DerivedResources, documents: Mapping[str, str], templates: Mapping[str, str]
) -> Mapping[str, str]:
    enabled = manifest.capabilities.enabled_names
    if enabled:
        raise RenderError("capability rendering is unavailable until an explicit capability module is approved")
    if not isinstance(manifest.smartpbx.capacity, int) or isinstance(manifest.smartpbx.capacity, bool) or not 1 <= manifest.smartpbx.capacity <= 4:
        raise RenderError("SmartPBX max calls must be between 1 and 4")
    title = manifest.display_name
    provider_profile, provider_requirements, provider_environment, provider_volumes = _provider_runtime_contract(manifest)
    compose = f'''services:
  {resources.smartpbx_service}:
    profiles: ["smartpbx"]
    build: .
    environment:
      ENABLE_SMARTPBX_WSS: "true"
      SMARTPBX_WS_TOKEN: ${{SMARTPBX_WS_TOKEN?required}}
      SMARTPBX_ACCOUNT_ID: ${{SMARTPBX_ACCOUNT_ID?required}}
      SMARTPBX_AUTH_HEADER_NAME: "{resources.wss_header}"
      SMARTPBX_MAX_CALLS: "{manifest.smartpbx.capacity}"
    ports: ["127.0.0.1:{resources.smartpbx_port}:8080"]
  {resources.website_service}:
    profiles: ["website-demo"]
    build: .
    command: python website_demo.py
    environment:
      WEBSITE_DEMO_ENABLED: "true"
      WEBSITE_DEMO_AGENT_ID: "{resources.slug}"
    ports: ["127.0.0.1:{resources.website_port}:8081"]
'''
    workflow_fragment = '''name: generated-agent-contract
jobs:
  smartpbx-generated-agent:
    runs-on: ubuntu-latest
    steps:
      - run: python -m pytest tests
      - run: docker build -t generated-agent-contract .
'''
    client_connect = f'''# Client Connect sheet

websocket_url: {resources.wss_url}
authentication_header: {resources.wss_header}
reachable_after_provisioning: false
activation_state: pending
'''
    infrastructure_variables = {
        "smartpbx_service": resources.smartpbx_service,
        "smartpbx_port": resources.smartpbx_port,
        "smartpbx_hostname": resources.smartpbx_hostname,
        "wss_header": resources.wss_header,
        "ghcr_repository": resources.ghcr_repository,
        "smartpbx_memory_limit": "1536m",
        "smartpbx_cpus": "2.0",
        "smartpbx_pids_limit": 256,
        "provider_requirements": "\n".join(provider_requirements),
        "provider_environment": provider_environment,
        "provider_volumes": provider_volumes,
        "tls_certificate_path": "/run/secrets/smartpbx-tls-fullchain.pem",
        "tls_certificate_key_path": "/run/secrets/smartpbx-tls-private-key.pem",
    }

    def infrastructure_template(name: str, fallback: str) -> str:
        return render_template_text(templates.get(f"infrastructure/{name}", fallback), infrastructure_variables)

    product_profile = {
        "display_name": manifest.display_name,
        "default_language": manifest.languages[0].code,
        "languages": {
            language.code: {
                "locale": language.locale,
                "stt": language.stt,
                "llm": language.llm,
                "tts": language.tts,
                "prompt_block": "Answer approved inquiries only.",
                "greeting": language.greeting,
            }
            for language in manifest.languages
        },
        "room_catalogue": {}, "room_aliases": {}, "transliterations": {}, "rates": {},
        "post_call_vocabulary": [], "knowledge_paths": ["/app/knowledge_docs/approved-facts.md"],
    }
    provider_lanes = ("provider_stt.py", "provider_llm.py", "provider_tts.py")
    missing_lanes = [name for name in provider_lanes if f"runtime/{name}.tmpl" not in templates]
    if missing_lanes:
        raise IncompleteTemplateError(f"INCOMPLETE_TEMPLATE: missing concrete provider lane: {missing_lanes[0]}")
    return {
        "AGENTS.md": "# Generated SmartPBX agent\n\nNo production provisioning or release is authorized by this tree.\n",
        "CLAUDE.md": "# Generated SmartPBX agent\n\nInquiry-only capability policy.\n",
        "README.md": f"# {title}\n\nGenerated inquiry-only SmartPBX backend.\n",
        "Dockerfile": infrastructure_template("Dockerfile.tmpl", "FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\nCMD [\"python\", \"server.py\"]\n"),
        ".dockerignore": ".env\n__pycache__/\n.pytest_cache/\n",
        ".env.example": infrastructure_template("env.example.tmpl", "ENABLE_SMARTPBX_WSS\nSMARTPBX_WS_TOKEN\nSMARTPBX_ACCOUNT_ID\nSMARTPBX_AUTH_HEADER_NAME\n"),
        "docker-compose.yml": infrastructure_template("docker-compose.yml.tmpl", compose),
        "requirements-prod.txt": infrastructure_template("requirements-prod.txt.tmpl", "# Standard-library runtime only.\n"),
        "requirements-prod.lock.txt": infrastructure_template("requirements-prod.lock.txt.tmpl", "# No runtime packages.\n"),
        "startup.py": templates.get("runtime/startup.py.tmpl", "app = object()\n"),
        "server.py": templates.get("runtime/server.py.tmpl", "ROUTES = ('/smartpbx/status', '/ws/v1/smartpbx/media')\nfrom smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry, SmartPBXSettings\n"),
        "smartpbx_diagnostics.py": templates.get("runtime/smartpbx_diagnostics.py.tmpl", "def redacted_status(active_sessions=0):\n    return {'active_sessions': active_sessions}\n"),
        "smartpbx_gateway.py": _python_gateway(resources),
        "smartpbx_protocol.py": templates.get("runtime/smartpbx_protocol.py.tmpl", "PROTOCOL_VERSION = 'smartpbx-ai-provider-v06'\n"),
        "smartpbx_session.py": templates.get("runtime/smartpbx_session.py.tmpl", "class InquirySession:\n    async def start(self): pass\n    async def finish(self): pass\n"),
        "smartpbx_transport.py": templates.get("runtime/smartpbx_transport.py.tmpl", "class SmartPBXMediaTransport: pass\n"),
        "product_profile.py": templates.get("runtime/product_profile.py.tmpl", "def load_product_profile(path): return object()\n"),
        "provider_adapters.py": templates.get("runtime/provider_adapters.py.tmpl", "class ConversationProviderAdapter: pass\n"),
        "provider_runtime.py": templates.get("runtime/provider_runtime.py.tmpl", "def bind_provider_adapter(*_args): raise RuntimeError('provider lane unavailable')\n"),
        "provider_stt.py": templates["runtime/provider_stt.py.tmpl"],
        "provider_llm.py": templates["runtime/provider_llm.py.tmpl"],
        "provider_tts.py": templates["runtime/provider_tts.py.tmpl"],
        "turn_engine.py": templates.get("runtime/turn_engine.py.tmpl", "class ConversationTurnEngine: pass\n"),
        "config/product_profile.json": json.dumps(product_profile, sort_keys=True, indent=2) + "\n",
        "config/provider_profile.json": json.dumps(provider_profile, sort_keys=True, indent=2) + "\n",
        "tools.py": "TOOL_REGISTRY = {}\n",
        "website_demo.py": "ROUTES = ('/voice/demo-incoming',)\n# Browser tokens are issued only by the shared approved issuer.\n",
        "nginx-smartpbx.conf": infrastructure_template("nginx-smartpbx.conf.tmpl", "location /smartpbx/status {}\nlocation /ws/v1/smartpbx/media {}\n"),
        f"nginx-{resources.smartpbx_service}.conf": infrastructure_template("nginx-smartpbx.conf.tmpl", "location /smartpbx/status {}\nlocation /ws/v1/smartpbx/media {}\n"),
        "scripts/deploy_smartpbx_image.sh": infrastructure_template("scripts/deploy_runtime_image.sh.tmpl", "#!/bin/sh\necho 'Manual release approval required.'\nexit 1\n"),
        "SMARTPBX_RUNBOOK.md": infrastructure_template("SMARTPBX_RUNBOOK.md.tmpl", "# Runbook\n\nThis generated artifact is review-only.\n"),
        "CLIENT_CONNECT.md": infrastructure_template("CLIENT_CONNECT.md.tmpl", client_connect),
        "demo-routing-activation.md": "# Future routing activation\n\nrelease_allowed: false\nstate: pending\nRequires separately approved backend health and shared routing activation.\n",
        "tests/test_generated_contract.py": "def test_contract_paths():\n    from server import ROUTES\n    assert '/smartpbx/status' in ROUTES\n",
        "tests/test_generated_security.py": '''import asyncio
import json

from smartpbx_gateway import SmartPBXGateway, SmartPBXSessionRegistry, SmartPBXSettings
from tools import TOOL_REGISTRY


class FakeWebSocket:
    def __init__(self, messages, header, token):
        self.headers = {header: token}
        self.messages = list(messages)
        self.accepted = False
        self.close_calls = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))

    async def receive_text(self):
        return json.dumps(self.messages.pop(0))


class Factory:
    def __init__(self):
        self.sessions = []

    async def __call__(self, start, _transport):
        self.sessions.append(start)
        return self

    async def start(self):
        return None

    async def finish(self):
        return None


def test_inquiry_only_registry_and_preaccept_authentication():
    assert TOOL_REGISTRY == {}

    async def exercise():
        settings = SmartPBXSettings("correct", "account-fixture")
        gateway = SmartPBXGateway(settings, SmartPBXSessionRegistry(1))
        wrong = FakeWebSocket([], settings.auth_header_name, "wrong")
        wrong_factory = Factory()
        await gateway.handle(wrong, wrong_factory)
        assert wrong.accepted is False
        assert wrong.close_calls == [(1008, "unauthorized")]
        assert wrong_factory.sessions == []
        valid = FakeWebSocket([
            {"event": "start", "start": {"accountId": "account-fixture"}},
            {"event": "stop"},
        ], settings.auth_header_name, "correct")
        valid_factory = Factory()
        await gateway.handle(valid, valid_factory)
        assert valid.accepted is True
        assert len(valid_factory.sessions) == 1

    asyncio.run(exercise())
''',
        ".github-workflow-fragment.yml": infrastructure_template("ci-runtime-review.yml.tmpl", workflow_fragment),
    } | {f"knowledge_docs/{filename}": f"# Approved knowledge\n\n{content}\n" for filename, content in documents.items()}


def _scan_outputs(files: Mapping[str, str]) -> None:
    for relative, content in files.items():
        for pattern in _IDENTITY_PATTERNS:
            if pattern.search(content):
                raise IdentityLeakError(f"identity leak in generated output: {relative}")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                raise IdentityLeakError(f"secret leak in generated output: {relative}")
        if "{{" in content or "}}" in content:
            raise IdentityLeakError(f"unresolved template marker in generated output: {relative}")
        if not relative.startswith("tools.py") and any(name in content for name in _BUSINESS_TOOLS):
            raise IdentityLeakError(f"business tool leak in generated output: {relative}")


def _derived_render_root(worktree: WorktreeHandle, manager: WorktreeManager, resources: DerivedResources) -> Path:
    """Derive the only permissible backend root from verified worktree ownership."""
    try:
        target = manager_owned_worktree_target(manager, worktree)
    except WorktreeConflictError as exc:
        raise RenderError(str(exc)) from exc
    root = target / resources.folder_identity
    current = target
    for component in Path(resources.folder_identity).parts:
        current = current / component
        if current.is_symlink():
            raise RenderError("generated backend root may not traverse a symlink")
        if current.exists() and not current.is_dir():
            raise RenderError("generated backend root component is not a directory")
    return root


def _reject_symlinked_tree(root: Path) -> None:
    if root.is_symlink():
        raise RenderError("generated backend cleanup may not traverse a symlink")
    if root.exists():
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                raise RenderError("generated backend cleanup may not traverse a symlink")


def _write_files(root: Path, files: Mapping[str, str]) -> tuple[str, ...]:
    if root.exists():
        raise RenderError(f"generated backend target already exists: {root}")
    try:
        for relative, content in files.items():
            target = root / relative
            if target.parent != root and root not in target.parents:
                raise RenderError("generated file escapes output root")
            current = root
            for component in Path(relative).parent.parts:
                current = current / component
                if current.is_symlink():
                    raise RenderError("generated backend path may not traverse a symlink")
                if current.exists() and not current.is_dir():
                    raise RenderError("generated backend path component is not a directory")
            target.parent.mkdir(parents=True, exist_ok=False) if not target.parent.exists() else None
            if target.is_symlink():
                raise RenderError("generated backend file may not be a symlink")
            target.write_text(content, encoding="utf-8")
    except BaseException:
        _reject_symlinked_tree(root)
        if root.exists():
            shutil.rmtree(root)
        raise
    return tuple(sorted(files))


def render_backend(
    manifest: AgentManifest,
    review: KnowledgeReview,
    resources: DerivedResources,
    worktree: WorktreeHandle,
    *,
    worktree_manager: WorktreeManager,
    state: GenerationState | None = None,
    template_allowlist: TemplateAllowlist | None = None,
    template_root: Path | None = None,
) -> RenderReport:
    """Render a deterministic backend tree from exact, verified template evidence.

    Omitting the explicit seam loads the checked-in partial allowlist, which
    raises ``INCOMPLETE_TEMPLATE`` until an approved client-neutral extraction
    provides a complete runtime.  Explicit templates are test-only synthetic
    fixtures and reports label their output non-deployable.
    """
    if template_allowlist is not None and not template_allowlist.template_version.startswith("synthetic-test-"):
        raise IncompleteTemplateError("INCOMPLETE_TEMPLATE: only synthetic fixture rendering is available")
    allowlist = template_allowlist or _load_default_allowlist()
    root = Path(template_root) if template_root is not None else _TEMPLATE_ROOT
    templates = _verify_supplied_templates(root, allowlist)
    if resources.slug != manifest.slug or resources.folder_identity != f"SmartPBX Agents/{manifest.slug}":
        raise RenderError("derived resources do not match manifest identity")
    facts = _review_facts(review, state, manifest)
    files = _files(manifest, resources, _knowledge_documents(review, facts), templates)
    _scan_outputs(files)
    rendered_root = _derived_render_root(worktree, worktree_manager, resources)
    names = _write_files(rendered_root, files)
    digest_input = "".join(f"{name}\0{files[name]}\0" for name in names).encode("utf-8")
    return RenderReport(
        output_dir=rendered_root,
        artifact_digest=hashlib.sha256(digest_input).hexdigest(),
        files=names,
        enabled_capabilities=manifest.capabilities.enabled_names,
        template_version=allowlist.template_version,
        review_digest=review.digest,
        synthetic=True,
        deployable=False,
        runtime_status="synthetic-structural-contract-only",
    )
