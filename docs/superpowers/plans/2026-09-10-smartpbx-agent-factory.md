# SmartPBX Agent Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build create-smartpbx-agent, a resumable, deterministic, fail-closed generator that turns an approved company manifest into three linked review-ready PRs: an isolated backend agent in full-voice-agent, a website demo/routing change in Taskforce_AI_Website, and SOPS-encrypted per-agent operations metadata in the private operations repository, without provisioning or deploying production.

**Architecture:** A standard-library-first Python package owns the manifest schema, provider catalogue, digest-bound state machine, collision-safe derived resources, knowledge review, frozen Kavya template renderer, secret-provider boundary, isolated-worktree generation, and PR orchestration. The package emits backend and website artifacts from explicit allowlists, never recursively clones Kavya, and invokes SOPS/Git/GitHub only through typed adapters with argument arrays. Foundation contracts and template provenance land first; only then may the knowledge, backend, and website renderers run independently. The SOPS/age lane is additionally gated on externally supplied operations prerequisites, and final PR creation is strictly sequential.

**Tech Stack:** Python 3.11, argparse, dataclasses, json, hashlib, pathlib, subprocess with shell=False, urllib, pypdf and python-docx through bounded adapters, SOPS with age, Docker/Compose/nginx templates, Git worktrees, GitHub CLI, Vite/React/TypeScript website.

**Validation boundary:** This Kavya sandbox does not run local pytest. Every local “red/green” command below is limited to `python3 -B -m py_compile`, `rg`, `cmp`, `git diff --check`, or other static/source checks. Behavioral pytest expectations are CI commands executed by the named GitHub workflows after a PR; the plan never claims those results from local static checks.

## Global Constraints

- Use a versioned, frozen template derived from the current deployed Kavya SmartPBX runtime; do not share mutable runtime code between customer agents.
- Before freezing template v1, prove that the deployed Kavya image revision and digest correspond to repository source; a branch name or local HEAD is not sufficient evidence.
- Every generated agent owns its hostnames, containers, ports, credentials, knowledge store, logs and health surfaces; website-demo and SmartPBX transports run as separate service profiles and never share an ingress surface.
- New agents are inquiry-only by default; booking, appointment creation, handover, WhatsApp, CRM, payment, post-call reporting, recording and transcript retention require explicit opt-in.
- Use one SOPS-encrypted YAML document per agent in a private operations repository; encrypt it to approved age recipients; commit only ciphertext and non-sensitive metadata.
- Accept PDF, DOCX, TXT and Markdown files plus approved public website URLs.
- Generation ends at three linked, review-ready PRs; provisioning and deployment are a later separately approved workflow.
- Generated SmartPBX WSS URL format is wss://smartpbx-<company-slug>.taskforceai.tech/ws/v1/smartpbx/media.
- The CLI is named create-smartpbx-agent and supports inspect, plan, generate, open-pr, resume and abandon; it has no deploy or provision command.
- Preserve Kavya's immutable image provenance, dedicated SmartPBX service, explicit environment allowlist, constant-time header-authenticated WSS, bounded admission/protocol inputs, privacy-safe diagnostics, nginx media/health/status surface, guarded deployment metadata, and post-deployment verification as generated contracts; generated v1 does not execute deployment.
- The generated inquiry-only tool registry must contain no business-action tool; the system prompt is not treated as a security boundary.
- Never copy a production .env wholesale; secrets never enter Git, command-line arguments, logs, prompts, reports or process listings.
- Generation occurs only in isolated worktrees; unrelated dirty primary worktrees are never reset, cleaned or aligned.
- Existing slugs, folders, ports, hostnames, containers and secret records are hard conflicts.
- A generation ID, stage digest, manifest digest and template/source provenance are recorded for every run; cryptographic secrets are generated once and reused on resume.
- No generated PR may activate automatic production deployment; merge and deployment are separate human decisions.
- Do not edit /home/dev/incoming/SmartPBX AI Provider - Version 07.pdf; use its reviewed protocol facts through the separate V07 compatibility plan.

---

## Current repository contracts and exact file map

The factory lives in the root of full-voice-agent, not inside Kavya, so a generated customer agent cannot import mutable Kavya application modules. The frozen template provenance source is read from Kavya at an approved commit and rendered into SmartPBX Agents/<slug>/ in a backend worktree. The generated root is intentionally not one of the existing deploy-on-push.yml agent-map entries; the generated agent's own blocking workflow is created in the same backend PR.

### Factory files in full-voice-agent

- Create: create-smartpbx-agent — executable wrapper for python3 -m smartpbx_agent_factory.
- Create: smartpbx_agent_factory/__main__.py — module entry point.
- Create: smartpbx_agent_factory/cli.py — subcommands, redacted output, exit codes.
- Create: smartpbx_agent_factory/model.py — immutable manifest, resource, secret and report dataclasses.
- Create: smartpbx_agent_factory/schema.py — strict manifest validation and canonical JSON digest.
- Create: smartpbx_agent_factory/catalogue.py — versioned provider/language capability catalogue.
- Create: smartpbx_agent_factory/state.py — generation stages, digest-bound approvals and resumable state.
- Create: smartpbx_agent_factory/resources.py — slug, identifiers, ports, hostnames and collision registry.
- Create: smartpbx_agent_factory/provenance.py — deployed-image/source matching and template file hash verification.
- Create: smartpbx_agent_factory/secrets.py — SecretProvider protocol and SOPS/age adapter.
- Create: smartpbx_agent_factory/knowledge.py — safe file/URL ingestion, source attribution and review report.
- Create: smartpbx_agent_factory/render.py — allowlisted backend rendering and leak scan.
- Create: smartpbx_agent_factory/website.py — website-card artifact renderer and dependency metadata.
- Create: smartpbx_agent_factory/operations.py — encrypted operations artifact renderer.
- Create: smartpbx_agent_factory/gitops.py — isolated worktrees, Git and GitHub adapter protocols.
- Create: smartpbx_agent_factory/orchestrator.py — stage transaction, resume/abandon, generation and linked PR flow.
- Create: smartpbx_agent_factory/template_v1/file_allowlist.json — exact source-to-template map and SHA-256 hashes.
- Create: smartpbx_agent_factory/template_v1/provider_catalogue.json — reviewed provider/language capabilities.
- Create: smartpbx_agent_factory/template_v1/runtime/ — tokenized, client-neutral runtime templates.
- Create: smartpbx_agent_factory/template_v1/infrastructure/ — Compose, nginx, Dockerfile, env example, runbook and Client Connect templates.
- Create: smartpbx_agent_factory/tests/ — factory unit, golden, security, state and orchestration tests.
- Modify: .gitignore — ignore only .smartpbx-generations/ state, never generated PR output.
- Create: .github/workflows/smartpbx-agent-factory.yml — blocking factory unit/security/golden test workflow.

### Generated backend PR paths

For manifest slug acme, the renderer writes exactly:

    SmartPBX Agents/acme/
    ├── AGENTS.md
    ├── CLAUDE.md
    ├── README.md
    ├── Dockerfile
    ├── .dockerignore
    ├── .env.example
    ├── docker-compose.yml
    ├── requirements-prod.txt
    ├── requirements-prod.lock.txt
    ├── server.py
    ├── smartpbx_diagnostics.py
    ├── smartpbx_gateway.py
    ├── smartpbx_mcp.py              (only when handover is explicitly enabled)
    ├── smartpbx_protocol.py
    ├── smartpbx_session.py
    ├── smartpbx_transport.py
    ├── tools.py                     (inquiry-only registry when no capability is enabled)
    ├── knowledge_docs/<source>.md
    ├── nginx-smartpbx.conf
    ├── nginx-smartpbx-acme.conf
    ├── scripts/deploy_smartpbx_image.sh
    ├── SMARTPBX_RUNBOOK.md
    ├── CLIENT_CONNECT.md
    ├── demo-routing-activation.md
    ├── tests/test_generated_contract.py
    ├── tests/test_generated_security.py
    └── .github-workflow-fragment.yml

The backend renderer refuses a source filename containing a path separator and writes only the allowlisted files. It never emits Kavya's hotel_info.txt, Hatton Hills, Yanolja/PMS, booking, WhatsApp, recording or transcript modules unless the manifest explicitly enables a supported capability and the capability renderer owns those files.

### Generated website PR paths

The website repository primary checkout is `/home/dev/full-voice-agent/Taskforce_AI_Website`; it is read-only input and is never edited, reset, cleaned, or committed by this plan. `WorktreeManager` fetches and pins its full remote SHA, then creates an isolated worktree under `/home/dev/worktrees/taskforce-ai-website-<generation-id>`. The live demo page is `components/pages/BookDemo.tsx`, and source verification must preserve its shared `TOKEN_URL = https://hattonhills.taskforceai.tech/api/voice-token` plus `?agent=<stable-slug>` flow. The one-time website integration adds in that isolated worktree:

- Modify: components/pages/BookDemo.tsx — merge generated cards into the existing Agent shape while preserving the shared token endpoint and `agent` query/connection parameter.
- Create: data/smartpbx-agents.generated.mjs — generated, non-secret card/routing data only.
- Create: scripts/validate-smartpbx-card.mjs — Node contract test for card schema, hostnames, language list and unavailable state.
- Modify: package.json — add "test:smartpbx": "node scripts/validate-smartpbx-card.mjs" once; repeated generations modify only the generated data file.

The generated card contains no `tokenUrl`, per-card demo host, WSS URL, Twilio credential, or SmartPBX token. It contains a stable `id` for a future shared HattonHills `DEMO_AGENT_HOSTS` entry, but v1 does not edit that live map. `BookDemo.tsx` continues to fetch the shared `/api/voice-token?agent=<id>` issuer and calls `device.connect({params: {agent: id, lang}})`; the website PR is review-only and blocked from release until a later approved routing activation proves backend health.

### Generated private operations PR paths

The private operations repository is selected only from SMARTPBX_OPERATIONS_REPOSITORY and must be an existing Git repository with an approved remote and age-recipient policy. For slug acme, the operations worktree contains:

    agents/acme/metadata.yaml
    agents/acme/secrets.sops.yaml

metadata.yaml contains non-sensitive template/source, hostname, port allocation, image repository, CI identifier, WSS header name, account-ID presence flag and rotation due date. secrets.sops.yaml is ciphertext only. The generator refuses to create this PR if the repository, recipient set, SOPS binary or age identity validation is unavailable.

## External prerequisites for the SOPS/age lane

Lane A cannot claim operational readiness until an operator supplies all of these prerequisites: the absolute path and canonical remote URL of an existing private operations repository via `SMARTPBX_OPERATIONS_REPOSITORY`; an owner/team for that repository; an approved age-recipient file and its review source; the named credential-source policy for each secret (provider/path/rotation owner, never plaintext); and available `sops` and `age` binaries. The parent must verify the remote URL, private visibility, recipient fingerprints, owner, and credential-source policy before dispatching Lane A. The factory may not create a repository, infer recipients, generate an age identity, or use a developer workstation secret as a substitute.

The implementation must fail closed with typed `OperationsPrerequisiteError` values for each missing prerequisite and tests must cover missing repository path, non-private/wrong remote, missing or unapproved recipients, unavailable binaries, missing owner, and missing credential-source policy. Fixture providers may prove ciphertext and redaction behavior, but they do not prove operational SOPS/age access. Until the prerequisites are supplied, report `OPS_PREREQUISITES_UNAVAILABLE`; do not claim the three-PR operational proof.

## Interface contracts shared by all Luna lanes

These interfaces land before any independent lane starts:

    from dataclasses import dataclass
    from pathlib import Path
    from typing import Mapping, Protocol, Sequence

    @dataclass(frozen=True)
    class AgentManifest:
        schema_version: int
        display_name: str
        public_name: str
        slug: str
        agent_name: str
        industry: str
        purpose: str
        audience: str
        profile: str
        timezone: str
        operating_hours: Mapping[str, str]
        technical_owner: str
        languages: tuple["LanguageProfile", ...]
        allowed_topics: tuple[str, ...]
        refused_topics: tuple[str, ...]
        pii_policy: "PiiPolicy"
        capabilities: "CapabilitySelection"
        knowledge_sources: tuple["KnowledgeSource", ...]
        smartpbx: "SmartPBXInput"
        operations: "OperationsInput"
        website_demo: "WebsiteDemoInput"

    class SecretProvider(Protocol):
        def fetch(self, name: str) -> str: ...
        def generate(self, name: str, *, length: int = 32) -> str: ...
        def encrypt_yaml(self, plaintext: bytes, *, path: Path) -> bytes: ...
        def validate(self) -> None: ...

    class KnowledgeBuilder(Protocol):
        def build(self, sources: Sequence["KnowledgeSource"], *, output_dir: Path) -> "KnowledgeReview": ...

    class BackendRenderer(Protocol):
        def render(self, manifest: AgentManifest, review: "KnowledgeReview", resources: "DerivedResources", *, output_dir: Path) -> "RenderReport": ...

    class WebsiteRenderer(Protocol):
        def render(self, manifest: AgentManifest, resources: "DerivedResources", *, backend_artifact_digest: str, backend_branch_sha: str, output_dir: Path) -> "WebsiteRenderReport": ...

    class PRProvider(Protocol):
        def open_pull_request(self, *, repository: str, branch: str, title: str, body: str) -> str: ...
        def comment_pull_request(self, *, pull_request_url: str, body: str) -> None: ...
        def update_pull_request_body(self, *, pull_request_url: str, body: str) -> None: ...

    class WorktreeManager(Protocol):
        def create(self, *, primary: Path, remote: str, revision: str, target: Path) -> "WorktreeHandle": ...
        def remove(self, handle: "WorktreeHandle") -> None: ...

`WorktreeManager.create` must (1) verify the primary exists and is a Git checkout, (2) reject a dirty primary or an existing/colliding target, (3) run `git -C <primary> fetch <remote> --prune`, (4) resolve `git -C <primary> rev-parse <remote>/main` to a full 40-hex SHA and reject anything else, and (5) run `git -C <primary> worktree add --detach <target> <full_sha>`. It performs no reset or clean on the primary. For the website, `primary` is `/home/dev/full-voice-agent/Taskforce_AI_Website`, `remote` is its configured `origin`, and `target` is `/home/dev/worktrees/taskforce-ai-website-<generation-id>`; every website edit, test, commit, and PR call uses that target. A dirty primary, non-full SHA, target collision, or failed fetch is a hard stop.

`WebsiteRenderer` receives only the immutable backend artifact digest and pinned backend branch SHA. It never receives or persists a backend PR URL; PR URLs are created only by sequential Task 10 after the backend PR exists. Lanes may add internal helpers, but they may not bypass these contracts, import generated customer code into Kavya, edit the website primary checkout, or pass plaintext secret values through a Git/PR API. Back-links are added only through the exact `comment_pull_request` or `update_pull_request_body` methods above; no invented provider method is permitted.

## Luna lane schedule

The parent agent must ask Terra to review this plan before dispatching Luna. After Terra approval, dispatch one Luna worker per lane through the approved Sol Claude Dispatcher, with each worker using its own isolated worktree and returning tests/commit evidence.

| Gate | Lane | Depends on | Output |
|---|---|---|---|
| Foundation 1 | Schema/state/resources | none | typed manifest, catalogue, state and collision contracts |
| Foundation 2 | Provenance/template seam | Foundation 1 | immutable V1 file map and client-neutral template interfaces |
| Parallel B | Knowledge | Foundations 1-2 | safe extraction, review digest and approval gate |
| Parallel C | Backend renderer | Foundations 1-2 plus Knowledge interface | isolated backend tree, tests, Docker/Compose/nginx/runbook artifacts |
| Parallel D | Website renderer | Foundations 1-2 plus WorktreeManager and website contract | isolated website worktree, shared-token card data, test/build gate |
| Gated A | Secrets + ops | Foundations 1-2 plus all external SOPS/age prerequisites | ciphertext operations artifact; operational lane remains blocked if prerequisites are absent |
| Join 1 | Verification | B-D and A when available | readiness report and immutable artifact/branch digests, no PR calls yet |
| Join 2 | PR coordinator | Join 1 and VERIFIED state | sequential backend, operations, then website PRs and explicit back-link comments/body updates |

Only B-D may run in parallel after Foundation 2 and the shared interfaces are immutable. Lane A does not run until its external prerequisites pass. PR creation is never parallel: backend opens first, operations second, website third, then the coordinator applies exact back-link comments/body updates in a deterministic final step. A lane that discovers a contract defect returns a blocked report; the parent resumes the foundation worker, updates the interface version, and reruns affected lanes.

## Phase 1 — Foundation contracts

### Task 1: Add the manifest schema and canonical digest

**Files:**
- Create: smartpbx_agent_factory/model.py
- Create: smartpbx_agent_factory/schema.py
- Create: smartpbx_agent_factory/tests/test_schema.py
- Create: smartpbx_agent_factory/tests/fixtures/acme-minimal.json
- Modify: .gitignore

**Interfaces:**
- Produces: parse_manifest(raw: Mapping[str, object]) -> AgentManifest and manifest_digest(manifest: AgentManifest) -> str.
- Rejects: unknown top-level keys, missing required values, invalid slug, unsupported profile, empty language catalogue, capability dependencies without fields, paths outside approved roots, and unknown provider/language combinations.

- [ ] **Step 1: Write the red tests.**

    def test_parse_manifest_rejects_unknown_keys():
        raw = json.loads(Path("smartpbx_agent_factory/tests/fixtures/acme-minimal.json").read_text())
        raw["unexpected"] = True
        with pytest.raises(ManifestError, match="unknown key: unexpected"):
            parse_manifest(raw)

    def test_manifest_digest_is_stable_and_excludes_no_fields():
        raw = json.loads(Path("smartpbx_agent_factory/tests/fixtures/acme-minimal.json").read_text())
        manifest = parse_manifest(raw)
        first = manifest_digest(manifest)
        second = manifest_digest(parse_manifest(json.loads(json.dumps(raw))))
        assert first == second
        assert re.fullmatch(r"[0-9a-f]{64}", first)

    def test_booking_requires_explicit_destination_and_pii_policy():
        raw = json.loads(Path("smartpbx_agent_factory/tests/fixtures/acme-minimal.json").read_text())
        raw["capabilities"]["booking"] = {"enabled": True}
        with pytest.raises(ManifestError, match="booking.destination"):
            parse_manifest(raw)

- [ ] **Step 2: Verify the red state.**

Run:

    python3 -B -m py_compile smartpbx_agent_factory/schema.py smartpbx_agent_factory/tests/test_schema.py

Expected: collection fails with ModuleNotFoundError for smartpbx_agent_factory.schema or the named functions are undefined.

- [ ] **Step 3: Implement the typed schema.**

Use frozen dataclasses and a canonical JSON encoder. The public parser must follow this shape:

    def parse_manifest(raw: Mapping[str, object]) -> AgentManifest:
        if not isinstance(raw, Mapping):
            raise ManifestError("manifest must be an object")
        unknown = sorted(set(raw) - TOP_LEVEL_KEYS)
        if unknown:
            raise ManifestError(f"unknown key: {unknown[0]}")
        manifest = _parse_dataclasses(raw)
        if manifest.profile not in {"demo", "production-intent"}:
            raise ManifestError("profile must be demo or production-intent")
        if manifest.capabilities.any_enabled and not manifest.pii_policy.explicit_consent:
            raise ManifestError("capabilities require pii_policy.explicit_consent")
        return manifest

    def manifest_digest(manifest: AgentManifest) -> str:
        payload = json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

The fixture must use fictional values (Acme Inquiry, slug acme-inquiry, en-US, no capabilities, and a local source path under the test fixture directory). It must not contain a production hostname, token, API key, caller number or Kavya identity.

- [ ] **Step 4: Verify GREEN and add state ignore.**

Run:

    python3 -B -m py_compile smartpbx_agent_factory/model.py smartpbx_agent_factory/schema.py smartpbx_agent_factory/tests/test_schema.py
    git diff --check

Expected: syntax/static checks pass; CI behavioral status pending. `git diff --check` emits no output. Add only /.smartpbx-generations/ to .gitignore.

- [ ] **Step 5: Commit.**

    git add smartpbx_agent_factory/model.py smartpbx_agent_factory/schema.py smartpbx_agent_factory/tests/test_schema.py smartpbx_agent_factory/tests/fixtures/acme-minimal.json .gitignore
    git commit -m "feat(factory): add strict onboarding manifest schema"

### Task 2: Add capability catalogue, state machine and resource allocator

**Files:**
- Create: smartpbx_agent_factory/catalogue.py
- Create: smartpbx_agent_factory/state.py
- Create: smartpbx_agent_factory/resources.py
- Create: smartpbx_agent_factory/template_v1/provider_catalogue.json
- Create: smartpbx_agent_factory/tests/test_catalogue.py
- Create: smartpbx_agent_factory/tests/test_state.py
- Create: smartpbx_agent_factory/tests/test_resources.py

**Interfaces:**
- Produces: CapabilityCatalogue.load(path), validate_pipeline(language, pipeline), GenerationState.start(), GenerationState.approve_knowledge(digest), GenerationState.approve_plan(digest), derive_resources(manifest, registry) -> DerivedResources, and AllocationRegistry.reserve(resources).
- DerivedResources contains slug, python_identifier, website_service, smartpbx_service, website_port, smartpbx_port, website_hostname, smartpbx_hostname, wss_url, wss_header, status_url, ghcr_repository, and ci_identifier.

- [ ] **Step 1: Write red tests for unsupported combinations, invalid transitions and collisions.**

    def test_catalogue_rejects_unverified_language_provider_pair():
        catalogue = CapabilityCatalogue.load(Path("smartpbx_agent_factory/template_v1/provider_catalogue.json"))
        with pytest.raises(CatalogueError, match="not verified"):
            catalogue.validate_pipeline("si", {"stt": "unverified", "llm": "claude", "tts": "elevenlabs"})

    def test_state_approval_is_digest_bound():
        state = GenerationState.start("gen-001", "manifest-digest")
        state.transition(Stage.KNOWLEDGE_REVIEW_REQUIRED)
        with pytest.raises(StateError, match="approval digest"):
            state.approve_knowledge("wrong")

    def test_resource_allocator_rejects_port_and_hostname_collision():
        registry = AllocationRegistry({"ports": [18081], "hostnames": ["smartpbx-acme.taskforceai.tech"]})
        manifest = fixture_manifest(slug="acme")
        with pytest.raises(ResourceConflict, match="port|hostname"):
            derive_resources(manifest, registry)

- [ ] **Step 2: Run the red tests.**

    python3 -B -m py_compile smartpbx_agent_factory/catalogue.py smartpbx_agent_factory/state.py smartpbx_agent_factory/resources.py smartpbx_agent_factory/tests/test_catalogue.py smartpbx_agent_factory/tests/test_state.py smartpbx_agent_factory/tests/test_resources.py

Expected: the new modules/imports are missing and the tests fail before any generated resource is written.

- [ ] **Step 3: Implement catalogue, state and allocator.**

The allocator must derive values, never ask the operator to choose them:

    def derive_resources(manifest: AgentManifest, registry: AllocationRegistry) -> DerivedResources:
        slug = validate_slug(manifest.slug)
        website_port = registry.next_free_port(start=18080, end=18999)
        smartpbx_port = registry.next_free_port(start=19080, end=19999, exclude={website_port})
        smartpbx_hostname = f"smartpbx-{slug}.taskforceai.tech"
        website_hostname = f"demo-{slug}.taskforceai.tech"
        registry.require_free_hostname(smartpbx_hostname)
        registry.require_free_hostname(website_hostname)
        header_slug = re.sub(r"[^A-Za-z0-9]", "-", manifest.agent_name).strip("-")[:48]
        return DerivedResources(
            slug=slug,
            python_identifier=slug.replace("-", "_"),
            website_service=f"smartpbx-{slug}-website",
            smartpbx_service=f"smartpbx-{slug}",
            website_port=website_port,
            smartpbx_port=smartpbx_port,
            website_hostname=website_hostname,
            smartpbx_hostname=smartpbx_hostname,
            wss_url=f"wss://{smartpbx_hostname}/ws/v1/smartpbx/media",
            wss_header=f"X-{header_slug}-SmartPBX-Token",
            status_url=f"https://{smartpbx_hostname}/smartpbx/status",
            ghcr_repository=f"ghcr.io/taskforce-ai-dev/smartpbx-{slug}",
            ci_identifier=f"smartpbx-{slug}",
        )

GenerationState allows only NEW -> INPUT_COLLECTED -> SECRETS_RESOLVED -> KNOWLEDGE_REVIEW_REQUIRED -> PLAN_REVIEW_REQUIRED -> GENERATED -> VERIFIED -> THREE_PRS_OPENED; any stage can become BLOCKED, and any pre-PR stage can become ABANDONED. Approval methods store the exact digest and refuse changed inputs.

- [ ] **Step 4: Verify GREEN.**

    python3 -B -m py_compile smartpbx_agent_factory/catalogue.py smartpbx_agent_factory/state.py smartpbx_agent_factory/resources.py

Expected: syntax/static checks pass; CI behavioral status pending. No files outside the test state directory are created.

- [ ] **Step 5: Commit the foundation contracts.**

    git add smartpbx_agent_factory/catalogue.py smartpbx_agent_factory/state.py smartpbx_agent_factory/resources.py smartpbx_agent_factory/template_v1/provider_catalogue.json smartpbx_agent_factory/tests/test_catalogue.py smartpbx_agent_factory/tests/test_state.py smartpbx_agent_factory/tests/test_resources.py
    git commit -m "feat(factory): add capability state and resource contracts"

### Task 3: Freeze template V1 provenance and source seams

**Files:**
- Create: smartpbx_agent_factory/provenance.py
- Create: smartpbx_agent_factory/template_v1/file_allowlist.json
- Create: smartpbx_agent_factory/template_v1/runtime/
- Create: smartpbx_agent_factory/template_v1/infrastructure/
- Create: smartpbx_agent_factory/tests/test_provenance.py
- Create: smartpbx_agent_factory/tests/test_template_allowlist.py
- Modify: .github/workflows/smartpbx-agent-factory.yml

**Interfaces:**
- Produces: ProvenanceEvidence, verify_deployed_image_source(image_ref, expected_revision, expected_digest), verify_template_files(path, allowlist), and render_template_text(template, variables).
- Requires an explicit full source revision and image digest. It rejects a branch name, local HEAD, mutable tag, missing OCI revision, mismatched digest, or file hash drift.

- [ ] **Step 1: Write red tests against the missing provenance seam.**

    def test_branch_name_is_not_accepted_as_template_revision():
        with pytest.raises(ProvenanceError, match="full 40-character revision"):
            validate_source_revision("main")

    def test_file_allowlist_rejects_unlisted_kavya_identity_file(tmp_path):
        (tmp_path / "hotel_info.txt").write_text("Hatton Hills")
        allowlist = {"server.py": "sha256:" + ("0" * 64)}
        with pytest.raises(ProvenanceError, match="not allowlisted"):
            verify_template_files(tmp_path, allowlist)

    def test_template_substitution_rejects_unknown_variable():
        with pytest.raises(TemplateError, match="unknown variable"):
            render_template_text("Hello {{company_name}}", {"agent_name": "A"})

- [ ] **Step 2: Run the red tests.**

    python3 -B -m py_compile smartpbx_agent_factory/provenance.py smartpbx_agent_factory/tests/test_provenance.py smartpbx_agent_factory/tests/test_template_allowlist.py

Expected: collection or named-function failures because provenance.py and template_v1 are absent.

- [ ] **Step 3: Inspect live provenance before recording the allowlist.**

Run from the full-voice-agent worktree:

    git rev-parse origin/main
    git ls-tree -r --name-only origin/main -- Kavya
    rg -n "org.opencontainers.image.revision|ghcr.io/taskforce-ai-dev/kavya|probe-kavya-image|build-kavya-image" .github Kavya

Expected: the operator supplies a 40-character reviewed source revision and its immutable GHCR digest from the strict probe/build evidence. The implementation must not infer either value from local HEAD, a branch, or a mutable image tag. The allowlist records the exact source revision, digest, OCI revision, protocol version, environment schema version, and SHA-256 for each copied file.

- [ ] **Step 4: Implement the explicit allowlist and token seam.**

The checked-in file_allowlist.json has a template_version, source_revision, image_digest, oci_revision, protocol_version, and files object mapping exact Kavya source paths to exact template paths and SHA-256 values. The runtime source seams expose typed variables such as agent_name, company_name, account_id, auth_header, ports, hostnames, and selected capabilities. Render only allowlisted files, replace exact double-brace variables, and reject unknown variables, path traversal, NUL bytes, symlink targets, and unresolved braces.

The frozen template must be client-neutral: no Hatton Hills, Kavya, hotel, PMS, TreeHouse, Mosvold, provider secret, caller identifier, or production host survives the leak scan. Shared code is copied from this file map only; a recursive copy of Kavya is forbidden.

- [ ] **Step 5: Verify GREEN and add the factory CI job.**

    python3 -B -m py_compile smartpbx_agent_factory/provenance.py smartpbx_agent_factory/tests/test_provenance.py smartpbx_agent_factory/tests/test_template_allowlist.py
    python3 -m compileall -q smartpbx_agent_factory
    git diff --check

Expected: syntax/static checks pass; CI behavioral status pending. No unresolved template marker appears outside the approved template source files.

Add smartpbx-agent-factory.yml with Python 3.11 and these blocking commands:

    python -m pip install --upgrade pip
    python -m pip install "pytest>=9.1.1" "pytest-asyncio>=1.4.0" pypdf python-docx
    # GitHub Actions only; never run this pytest command in the Kavya sandbox.
    python -m pytest smartpbx_agent_factory/tests --timeout=300

- [ ] **Step 6: Commit the foundation/template gate.**

    git add smartpbx_agent_factory/provenance.py smartpbx_agent_factory/template_v1 smartpbx_agent_factory/tests/test_provenance.py smartpbx_agent_factory/tests/test_template_allowlist.py .github/workflows/smartpbx-agent-factory.yml
    git commit -m "feat(factory): freeze SmartPBX template provenance seam"

After this commit, Terra reviews the two plans and the foundation interfaces. Only after Terra approval may Luna lanes A-D begin.

## Phase 2 — Independent Luna implementation lanes

### Lane A / Task 4: Implement least-privilege secrets and encrypted operations artifacts

**Files:**
- Create: smartpbx_agent_factory/secrets.py
- Create: smartpbx_agent_factory/operations.py
- Create: smartpbx_agent_factory/tests/test_secrets.py
- Create: smartpbx_agent_factory/tests/test_operations.py
- Create: smartpbx_agent_factory/tests/fixtures/age-recipients.txt

**Interfaces:**
- Consumes: AgentManifest, DerivedResources, SecretProvider, and frozen template version/source evidence.
- Produces: SopsAgeSecretProvider, SecretBundle, render_operations_artifacts(manifest, resources, provider, output_dir), and a redacted SecretAudit.
- SecretProvider exposes named fetch/generate/encrypt_yaml/validate only; it never returns a vault export, runs shell evaluation, or accepts an arbitrary command.
- Operational prerequisite input is a validated `OperationsPrerequisites` record containing the existing private repository path/canonical remote, owner, approved recipient fingerprints/file, credential-source policy, and absolute `sops`/`age` binaries. Missing or unapproved values raise `OperationsPrerequisiteError` before any output or PR call.

- [ ] **Step 1: Write red security tests.**

    def test_generated_token_is_reused_for_resume_without_printing_value(tmp_path):
        provider = FakeSecretProvider()
        first = provider.generate("wss_token", length=32)
        second = provider.generate("wss_token", length=32)
        assert first == second
        report = provider.audit_report()
        assert first not in json.dumps(report)

    def test_operations_output_contains_ciphertext_only(tmp_path):
        provider = FakeSecretProvider(ciphertext=b"sops:\n  age: encrypted")
        report = render_operations_artifacts(fixture_manifest(), fixture_resources(), provider, tmp_path)
        assert (tmp_path / "agents/acme-inquiry/secrets.sops.yaml").read_bytes() == provider.ciphertext
        assert report.plaintext_paths == ()
        assert "api-key" not in (tmp_path / "agents/acme-inquiry/metadata.yaml").read_text()

    def test_missing_age_recipient_blocks_before_output(tmp_path):
        provider = FakeSecretProvider(error=SecretError("age recipient validation failed"))
        with pytest.raises(SecretError, match="age recipient"):
            render_operations_artifacts(fixture_manifest(), fixture_resources(), provider, tmp_path)
        assert not list(tmp_path.rglob("*.sops.yaml"))

- [ ] **Step 2: Run the red security tests.**

    python3 -B -m py_compile smartpbx_agent_factory/secrets.py smartpbx_agent_factory/operations.py smartpbx_agent_factory/tests/test_secrets.py smartpbx_agent_factory/tests/test_operations.py

Expected: missing provider/renderer symbols or failing assertions; no plaintext output is accepted.

- [ ] **Step 3: Implement the provider boundary.**

Use a list-form subprocess call with shell=False and a restrictive temporary file. `validate()` first checks every external prerequisite named above, private remote visibility through the provider adapter, recipient fingerprints, owner, credential-source policy, and binary versions. SopsAgeSecretProvider.fetch validates a named key and retrieves only that key; generate uses secrets.token_urlsafe and caches the value in the generation state; encrypt_yaml runs `sops --encrypt` with approved age recipients and returns ciphertext bytes; no repository is created. Redact subprocess output in all exceptions and remove plaintext in a finally block.

- [ ] **Step 4: Implement operations output and scan.**

render_operations_artifacts writes agents/acme-inquiry/metadata.yaml with only non-secret fields and encrypts a minimal YAML object containing named secrets. It scans the resulting tree for every generated secret value, credential-pattern regex, PEM marker, JWT marker, and unencrypted sops input. A scan failure removes only this generation's operations files and raises SecretLeakError.

- [ ] **Step 5: Verify the fixture lane and commit; do not claim operational proof.**

    python3 -B -m py_compile smartpbx_agent_factory/secrets.py smartpbx_agent_factory/operations.py smartpbx_agent_factory/tests/test_secrets.py smartpbx_agent_factory/tests/test_operations.py
    python3 -B -m py_compile smartpbx_agent_factory/schema.py smartpbx_agent_factory/resources.py
    git diff --check
    git add smartpbx_agent_factory/secrets.py smartpbx_agent_factory/operations.py smartpbx_agent_factory/tests/test_secrets.py smartpbx_agent_factory/tests/test_operations.py smartpbx_agent_factory/tests/fixtures/age-recipients.txt
    git commit -m "feat(factory): add encrypted SmartPBX operations artifacts"

Expected: syntax/static checks pass; CI behavioral status pending. No secret value occurs in captured output, Git diff, or metadata. Without the operator-supplied private repository, approved recipients, owner, credential-source policy, and binaries, the real lane remains `OPS_PREREQUISITES_UNAVAILABLE` and no three-PR operational proof is claimed.

### Lane B / Task 5: Implement safe knowledge ingestion and approval

**Files:**
- Create: smartpbx_agent_factory/knowledge.py
- Create: smartpbx_agent_factory/tests/test_knowledge.py
- Create: smartpbx_agent_factory/tests/fixtures/knowledge/faq.txt
- Create: smartpbx_agent_factory/tests/fixtures/knowledge/contradictory.md
- Create: smartpbx_agent_factory/tests/fixtures/knowledge/poisoned.txt

**Interfaces:**
- Consumes: tuple[KnowledgeSource, ...] from the validated manifest and an operator-selected output directory.
- Produces: KnowledgeReview with facts, source locations, conflicts, missing facts, sensitive findings, inaccessible sources, digest, and approval status; KnowledgeBuilder.build(sources, output_dir).
- It never executes text extracted from a file or website. Instructions inside source material are data and appear only in the review report.

- [ ] **Step 1: Write red safety tests.**

    def test_path_traversal_and_symlink_sources_are_rejected(tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
        with pytest.raises(KnowledgeError, match="symlink"):
            KnowledgeBuilderImpl(max_bytes=1000).build((local_source(link),), output_dir=tmp_path / "out")

    def test_url_redirect_cannot_escape_allowlisted_origin(http_server):
        source = url_source(http_server.url("/redirect"), origins=("allowed.example",))
        with pytest.raises(KnowledgeError, match="origin"):
            KnowledgeBuilderImpl().build((source,), output_dir=http_server.tmp_path / "out")

    def test_poisoned_instructions_are_reported_as_data(tmp_path):
        review = KnowledgeBuilderImpl().build((local_source(fixture("poisoned.txt")),), output_dir=tmp_path)
        assert review.instruction_findings == ("source text contains instruction-like content",)
        assert not review.executed_instructions

    def test_conflict_requires_digest_bound_approval(tmp_path):
        review = KnowledgeBuilderImpl().build((local_source(fixture("contradictory.md")),), output_dir=tmp_path)
        assert review.conflicts
        with pytest.raises(KnowledgeApprovalRequired):
            review.require_approved("wrong-digest")

- [ ] **Step 2: Run the red tests.**

    python3 -B -m py_compile smartpbx_agent_factory/knowledge.py smartpbx_agent_factory/tests/test_knowledge.py

Expected: missing builder/error symbols or failed safety assertions.

- [ ] **Step 3: Implement bounded extraction.**

Use pathlib.open for PDF/TXT/Markdown and python-docx for DOCX; enforce a per-file byte limit of 10 MiB, a total source limit of 50 MiB, maximum 8 URL redirects, a 10-second connect/read timeout, content-type allowlist, and a normalized text limit of 200,000 characters per source. Resolve local paths with strict realpath checks under the manifest-approved source root and reject every symlink. Use a custom urllib redirect handler that checks each hop's hostname and optional path prefix before fetching.

Emit one Markdown file per approved source under knowledge_docs with source URI, owner, effective date, classification, section anchors, and extracted text. Compute review.digest over normalized facts plus source metadata, excluding timestamps. Detect duplicate facts, contradictions, PII patterns, missing owner/effective date, and source-free claims. The review approval method compares the exact digest stored in GenerationState.

- [ ] **Step 4: Verify GREEN and commit the lane.**

    python3 -B -m py_compile smartpbx_agent_factory/knowledge.py smartpbx_agent_factory/tests/test_knowledge.py
    git diff --check
    git add smartpbx_agent_factory/knowledge.py smartpbx_agent_factory/tests/test_knowledge.py smartpbx_agent_factory/tests/fixtures/knowledge
    git commit -m "feat(factory): add bounded knowledge review builder"

Expected: syntax/static checks pass; CI behavioral status pending. No network request is made to an unallowlisted origin.

### Lane C / Task 6: Render the isolated backend and generated contract tests

**Files:**
- Create: smartpbx_agent_factory/render.py
- Create: smartpbx_agent_factory/tests/test_render.py
- Create: smartpbx_agent_factory/tests/test_generated_tree.py
- Create: smartpbx_agent_factory/template_v1/runtime/*.tmpl
- Create: smartpbx_agent_factory/template_v1/infrastructure/*.tmpl
- Create: smartpbx_agent_factory/tests/golden/
- Modify: .github/workflows/smartpbx-agent-factory.yml

**Interfaces:**
- Consumes: approved AgentManifest, approved KnowledgeReview, DerivedResources, ProvenanceEvidence and SecretProvider-generated secret names (never secret values).
- Produces: RenderReport, a complete backend tree under SmartPBX Agents/<slug>/, and the agent-specific blocking workflow fragment.
- render_backend must be deterministic for the same template revision, approved manifest and review digest; timestamps and cryptographic values are injected only into encrypted operations metadata.

- [ ] **Step 1: Write red renderer and isolation tests.**

    def test_inquiry_only_render_has_no_business_tools(tmp_path):
        report = render_backend(fixture_manifest(capabilities={}), fixture_review(), fixture_resources(), tmp_path)
        tools = (tmp_path / "SmartPBX Agents/acme-inquiry/tools.py").read_text()
        assert "create_booking" not in tools
        assert "transfer_to_human" not in tools
        assert "hangup_call" not in tools
        assert report.enabled_capabilities == ()

    def test_renderer_rejects_kavya_identity_leak(tmp_path):
        review = fixture_review(facts=("Hatton Hills is a hotel",))
        with pytest.raises(IdentityLeakError, match="Hatton Hills"):
            render_backend(fixture_manifest(), review, fixture_resources(), tmp_path)

    def test_backend_has_two_separate_service_profiles_and_bidirectional_isolation(tmp_path):
        render_backend(fixture_manifest(profile="demo"), fixture_review(), fixture_resources(), tmp_path)
        compose = (tmp_path / "SmartPBX Agents/acme-inquiry/docker-compose.yml").read_text()
        assert "smartpbx-acme-inquiry-website" in compose
        assert "smartpbx-acme-inquiry" in compose
        assert "SMARTPBX_WS_TOKEN" in compose
        assert "TWILIO_AUTH_TOKEN" not in compose.split("smartpbx-acme-inquiry:", 1)[1]
        assert "/smartpbx/status" in smartpbx_routes(tmp_path)
        assert "/voice/demo-incoming" not in smartpbx_routes(tmp_path)
        assert "TWILIO_AUTH_TOKEN" not in smartpbx_environment(tmp_path)
        assert "/voice/demo-incoming" in twilio_routes(tmp_path)
        assert "/api/voice-token" not in twilio_routes(tmp_path)
        assert "/smartpbx/status" not in twilio_routes(tmp_path)
        assert "SMARTPBX_WS_TOKEN" not in twilio_environment(tmp_path)
        assert "TWILIO_API_KEY_SID" not in twilio_environment(tmp_path)
        assert "TWILIO_API_KEY_SECRET" not in twilio_environment(tmp_path)

    def test_generated_contract_rejects_wrong_wss_credentials_before_accept(tmp_path):
        render_backend(fixture_manifest(), fixture_review(), fixture_resources(), tmp_path)
        # Use the current Kavya test seam: FakeWebSocket + gateway.handle().
        # The invalid path must close before accept; the valid path accepts and
        # consumes the normal start/stop lifecycle.
        gateway, registry, wrong, factory = run_generated_gateway(
            tmp_path, messages=[], token="wrong"
        )
        assert wrong.accepted is False
        assert wrong.close_calls == [(1008, "unauthorized")]
        assert factory.sessions == []
        _, _, valid, _ = run_generated_gateway(
            tmp_path, messages=[START, {"event": "stop"}], token="correct"
        )
        assert valid.accepted is True

- [ ] **Step 2: Run the red tests.**

    python3 -B -m py_compile smartpbx_agent_factory/render.py smartpbx_agent_factory/tests/test_render.py smartpbx_agent_factory/tests/test_generated_tree.py

Expected: missing renderer symbols or missing generated files.

- [ ] **Step 3: Implement allowlisted backend rendering.**

Render only the frozen file map and explicit capability modules. The inquiry-only baseline includes the strict V07 protocol parser, gateway, transport, diagnostics, session adapter, server, client-neutral prompt, empty tool registry, knowledge docs, Dockerfile, explicit Compose environment allowlist, nginx media/health/status routes, guarded deployment helper metadata, runbook, Client Connect sheet, a redacted `demo-routing-activation.md` checklist, and tests. Website/demo mode adds a separate downstream Twilio call/websocket ingress profile implementing the existing `/voice/demo-incoming` contract; it does not mint browser tokens. The shared HattonHills service remains the sole `VoiceGrant`/`/api/voice-token` issuer, and the SmartPBX/downstream profile receives no Twilio API-key SID/secret unless a separately enabled capability explicitly owns that requirement.

The generated backend contract tests must assert:

    expected_wss = "wss://smartpbx-acme-inquiry.taskforceai.tech/ws/v1/smartpbx/media"
    assert client_connect["websocket_url"] == expected_wss
    assert client_connect["authentication_header"] == "X-Acme-Inquiry-SmartPBX-Token"
    assert client_connect["reachable_after_provisioning"] is False
    assert "SMARTPBX_WS_TOKEN" in env_example
    assert "SMARTPBX_WS_TOKEN=" not in env_example

`run_generated_gateway` is only a test fixture that imports the generated `SmartPBXGateway`, `SmartPBXSessionRegistry`, settings, and factory, then uses the same `FakeWebSocket` shape and `gateway.handle(socket, factory)` call already present in `Kavya/tests/test_smartpbx_gateway.py`. It must not add an application method such as `accepts_header`. The test records `accepted` and `close_calls`; invalid authentication must produce `(1008, "unauthorized")` before `accept()`, while valid authentication sends `START` and `stop` and proves the accepted lifecycle.

The render scan checks every output file for source-client identities, hostnames, account IDs, phone numbers, API-key formats, private keys, secret values, and unresolved template markers. It rejects recursive source copies, business-tool names outside enabled modules, and any route that exposes Twilio credentials on the SmartPBX service.

- [ ] **Step 4: Add generated agent CI registration.**

The renderer writes `.github-workflow-fragment.yml` only as data for the root workflow. The backend PR creates or updates exactly `.github/workflows/smartpbx-generated-agents.yml` with:

    name: smartpbx-generated-agents
    on:
      pull_request:
        paths: ["SmartPBX Agents/**", "smartpbx_agent_factory/**", ".github/workflows/smartpbx-generated-agents.yml"]
      push:
        branches: [main]
        paths: ["SmartPBX Agents/**", "smartpbx_agent_factory/**", ".github/workflows/smartpbx-generated-agents.yml"]

It exposes one stable required job/check name, `smartpbx-generated-agent`, discovers the changed `SmartPBX Agents/<slug>` directory, runs its behavioral pytest suite in GitHub CI, builds/imports the image, and runs the disposable WSS lifecycle contract without production secrets. The workflow must not declare `workflow_call`, invoke `gh workflow run`, call any deploy workflow, or contain Docker push/deploy/provision steps. Add a source test asserting the exact PR/push triggers, path filters, stable job name, and absence of deploy calls. Preserve the generic `.github/workflows/deploy-on-push.yml` and `.github/workflows/deploy.yml` agent-map exclusion: `SmartPBX Agents/**` remains outside that generic deploy map and no generated PR may alter it to activate deployment.

- [ ] **Step 5: Verify golden determinism and commit the lane.**

    python3 -B -m py_compile smartpbx_agent_factory/render.py smartpbx_agent_factory/tests/test_render.py smartpbx_agent_factory/tests/test_generated_tree.py
    python3 -B -m py_compile smartpbx_agent_factory/schema.py smartpbx_agent_factory/provenance.py
    git diff --check
    git add smartpbx_agent_factory/render.py smartpbx_agent_factory/tests/test_render.py smartpbx_agent_factory/tests/test_generated_tree.py smartpbx_agent_factory/template_v1/runtime smartpbx_agent_factory/template_v1/infrastructure smartpbx_agent_factory/tests/golden .github/workflows/smartpbx-agent-factory.yml
    git commit -m "feat(factory): render isolated SmartPBX backend agents"

Expected: the listed syntax/static checks pass; CI behavioral status pending. These commands do not establish golden determinism, identity-scan, or tool-isolation results; record those claims only after running the exact generated-tree comparison and scan commands.

### Lane D / Task 7: Render the linked website demo package

**Files:**
- Create: smartpbx_agent_factory/website.py
- Create: smartpbx_agent_factory/tests/test_website.py
- Modify: `<website-worktree>/components/pages/BookDemo.tsx` where `<website-worktree>` is `/home/dev/worktrees/taskforce-ai-website-<generation-id>`
- Create: `<website-worktree>/data/smartpbx-agents.generated.mjs`
- Create: `<website-worktree>/scripts/validate-smartpbx-card.mjs`
- Modify: `<website-worktree>/package.json`

**Interfaces:**
- Consumes: manifest.website_demo, manifest.languages, DerivedResources, the source-verified shared HattonHills token contract, the immutable backend artifact digest, and the pinned backend branch SHA. It does not consume or persist a backend PR URL.
- Produces: `render_website_artifacts(output_dir, backend_artifact_digest, backend_branch_sha)` with public card metadata, stable agent slug, language controls, dependency metadata containing only immutable digests, loading/unavailable fallback, and no token URL, per-card host, WSS URL, or secret.

- [ ] **Step 1: Write red tests for the current website seam.**

    def test_website_artifact_has_public_fields_only(tmp_path):
        render_website_artifacts(fixture_manifest(profile="demo"), fixture_resources(), backend_artifact_digest="a" * 64, backend_branch_sha="b" * 40, output_dir=tmp_path)
        source = (tmp_path / "data/smartpbx-agents.generated.mjs").read_text()
        assert "wss://" not in source
        assert "SMARTPBX_WS_TOKEN" not in source
        assert "api-key" not in source
        assert "acme-inquiry" in source

    def test_website_artifact_uses_shared_hattonhills_token_contract(tmp_path):
        result = render_website_artifacts(fixture_manifest(profile="demo"), fixture_resources(), backend_artifact_digest="a" * 64, backend_branch_sha="b" * 40, output_dir=tmp_path)
        source = (tmp_path / "data/smartpbx-agents.generated.mjs").read_text()
        assert result.dependency.backend_artifact_digest == "a" * 64
        assert result.dependency.backend_branch_sha == "b" * 40
        assert not hasattr(result.dependency, "backend_pr")
        assert result.dependency.release_order == ("backend", "website")
        assert 'id: "acme-inquiry"' in source
        assert "tokenUrl" not in source
        assert "backendHost" not in source
        assert "DEMO_AGENT_HOSTS" not in source
        assert "github.com/example/pr" not in source

    def test_pending_card_is_not_rendered_but_active_card_is(tmp_path):
        render_website_artifacts(fixture_manifest(profile="demo"), fixture_resources(), backend_artifact_digest="a" * 64, backend_branch_sha="b" * 40, output_dir=tmp_path)
        source = (tmp_path / "data/smartpbx-agents.generated.mjs").read_text()
        book_demo = (tmp_path / "components/pages/BookDemo.tsx").read_text()
        assert 'releaseState: "pending"' in source
        assert 'releaseState === "active"' in book_demo
        cards = ({"id": "pending-agent", "releaseState": "pending"}, {"id": "active-agent", "releaseState": "active"})
        rendered_cards = [card for card in cards if card["releaseState"] == "active"]
        assert [card["id"] for card in rendered_cards] == ["active-agent"]
        assert "pending-agent" not in [card["id"] for card in rendered_cards]
        assert "active-agent" in [card["id"] for card in rendered_cards]

- [ ] **Step 2: Run the red tests.**

    python3 -B -m py_compile smartpbx_agent_factory/website.py smartpbx_agent_factory/tests/test_website.py
    rg -n 'tokenUrl|backendHost|DEMO_AGENT_HOSTS|hattonhills.taskforceai.tech/api/voice-token|agent=' smartpbx_agent_factory/website.py

Expected: missing website renderer symbols.

- [ ] **Step 3: Implement the one-time website integration.**

Before editing, source-verify the read-only references `/home/dev/full-voice-agent/HattonHills/server.py:1060-1140,1256-1340`, `/home/dev/full-voice-agent/HattonHills/nginx.conf`, `/home/dev/full-voice-agent/HattonHills/AGENTS.md:589-598`, and `/home/dev/full-voice-agent/Taskforce_AI_Website/components/pages/BookDemo.tsx:29-31,375-430`. Do not modify `HattonHills/server.py`, its live `DEMO_AGENT_HOSTS` map, or any live routing/configuration in v1. Preserve the existing shared `TOKEN_URL = https://hattonhills.taskforceai.tech/api/voice-token`; the browser requests `?agent=${encodeURIComponent(agent.id)}`, creates a Twilio `Device` from the shared short-lived `VoiceGrant`, and connects with `{params: {agent: agent.id, lang}}`. The shared TwiML application sends the call to `/voice/demo-incoming`, which later uses `DEMO_AGENT_HOSTS` to redirect by agent. Do not create a per-card `tokenUrl`, demo host, WSS URL, or token contract.

`BookDemo.tsx` imports `SMARTPBX_AGENT_CARDS`, filters them with `SMARTPBX_AGENT_CARDS.filter((card) => card.releaseState === "active")`, maps only those active cards into the existing Agent shape, and appends them to the existing list. The generated module exports only `id`, `releaseState`, display/description fields, supported `langs`, call copy, and steps. Every v1 generated card has `releaseState: "pending"`; pending cards are absent from the rendered/callable `AGENTS` list, so merging the website PR cannot expose or call them. A card id becomes review-valid only when a later approved routing activation changes the card state to `active`, the redacted checklist records the future `DEMO_AGENT_HOSTS` entry and generated `/voice/demo-incoming` target, and backend health is proven. Token fetch failures use the existing user-visible unavailable message and return to idle; no browser code receives a WSS token, MCP key, or Twilio credential.

The generated module exports only this public shape:

    export const SMARTPBX_AGENT_CARDS = Object.freeze([
      {
        id: "acme-inquiry",
        releaseState: "pending",
        brand: "Acme Inquiry",
        agentName: "Ava",
        role: "Information Assistant",
        location: "Online",
        description: "Answers approved Acme information questions.",
        images: [],
        trainedOn: ["approved company information"],
        langs: ["en"],
        callLabel: "Call Acme Inquiry",
        askHint: "approved company information",
        steps: [{bold: "Ask a question.", rest: "Use the approved company information."}]
      }
    ]);

The actual generator emits only public card data; it refuses token URLs, demo hosts, WSS URLs, credentials, and backend secrets in website data. The website package depends on backend readiness and does not edit Hostinger or deploy.

The generated backend keeps distinct `twilio` and `smartpbx` service profiles. HattonHills remains the sole browser token issuer: only its existing service may read the source-verified `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, and `TWILIO_TWIML_APP_SID` names, construct `VoiceGrant(outgoing_application_sid=app_sid, incoming_allow=False)`, and serve `/api/voice-token`. Generated agents do not expose `/api/voice-token` and receive none of those token-minting credentials. Their downstream Twilio profile implements only the existing redirected `/voice/demo-incoming` and call/websocket ingress contract; Twilio REST credentials remain absent unless a separately enabled capability genuinely requires them. Source-verify exact env names in `HattonHills/.env.example` and the server module; do not invent aliases.

The shared HattonHills service's CORS allowlist and rate limits remain read-only references in `HattonHills/server.py` (the `_CORS_ALLOWED_ORIGINS` and IP sliding-window checks around lines 1060-1107) plus webhook limits in `HattonHills/nginx.conf`; v1 does not modify either. Add generated-tree tests that assert the generated service has no `/api/voice-token` route and no token-minting Twilio credentials, while it retains `/voice/demo-incoming` downstream ingress and SmartPBX isolation. Add read-only contract tests against a fake HattonHills seam for the existing shared issuer and redirected `agent`/`lang` behavior; these tests do not change `DEMO_AGENT_HOSTS`, call Twilio, use Hostinger, or touch production DNS.

`demo-routing-activation.md` is a redacted, non-operational checklist containing the generated agent id, proposed host target, immutable backend artifact/branch digests, required `/voice/demo-incoming` health checks, and an explicit “pending approved routing activation” status. It contains no credential, live token, or claim that `DEMO_AGENT_HOSTS` has been changed. The website PR may be opened for review, but its acceptance gate must fail closed until a separately approved release action adds the mapping after backend health and ingress checks pass; that release action is outside this plan.

The later approved activation is atomic at the product-contract level: after backend health and `/voice/demo-incoming` ingress proof, the operator updates the shared HattonHills `DEMO_AGENT_HOSTS` route and changes the website card's `releaseState` from `pending` to `active` in the same reviewed release sequence. Either half missing leaves the card hidden and non-callable; this factory plan does not perform that activation.

- [ ] **Step 4: Add website contract/build tests.**

`scripts/validate-smartpbx-card.mjs` imports the generated module and exits nonzero unless every card has a stable lowercase id, `releaseState` exactly `pending` or `active`, non-empty display fields, supported language values, no secret-looking field, no `tokenUrl`, no demo host, no WSS URL, and an unavailable-state-compatible card. Its contract test must supply one pending and one active fixture and prove only the active card reaches the rendered/callable list. `BookDemo.tsx` must contain the same active-state filter. `package.json` adds `test:smartpbx` once. Run only in the isolated worktree:

    cd /home/dev/worktrees/taskforce-ai-website-<generation-id>
    npm run test:smartpbx
    npm run build

Expected: syntax/static checks pass; CI behavioral status pending. GitHub CI will run the validator and Vite/sitemap/prerender build in the isolated website worktree.

- [ ] **Step 5: Verify and commit the lane in the website worktree.**

    cd /home/dev/worktrees/taskforce-ai-website-<generation-id>
    git diff --check
    git add components/pages/BookDemo.tsx data/smartpbx-agents.generated.mjs scripts/validate-smartpbx-card.mjs package.json
    git commit -m "feat(demo): add generated SmartPBX demo card contract"

Expected: only the website integration and generated non-secret card are staged; no token, API key, WSS URL, or backend secret appears in the diff.

## Phase 3 — Transaction orchestration and verification

### Task 8: Implement the CLI, resumable transaction and isolated worktrees

**Files:**
- Create: smartpbx_agent_factory/cli.py
- Create: smartpbx_agent_factory/__main__.py
- Create: create-smartpbx-agent
- Create: smartpbx_agent_factory/gitops.py
- Create: smartpbx_agent_factory/orchestrator.py
- Create: smartpbx_agent_factory/tests/test_cli.py
- Create: smartpbx_agent_factory/tests/test_orchestrator.py
- Create: smartpbx_agent_factory/tests/test_gitops.py

**Interfaces:**
- Produces commands inspect, plan, generate, open-pr, resume generation-id, and abandon generation-id with exit codes 0 success, 2 invalid input, 3 blocked gate, 4 infrastructure failure, and 5 dirty/conflicting worktree.
- GenerationOrchestrator.plan(manifest_path), generate(generation_id), resume(generation_id), abandon(generation_id), and open_pr(generation_id) are the only command implementations.
- WorktreeManager.create(primary, remote, revision, target) and remove(handle) operate only inside an explicit temporary root and never invoke shell interpretation. The website primary `/home/dev/full-voice-agent/Taskforce_AI_Website` is checked for a clean tree, fetched, resolved to a full remote SHA, and left untouched; edits occur only in `/home/dev/worktrees/taskforce-ai-website-<generation-id>`.

- [ ] **Step 1: Write red CLI/state tests.**

    def test_cli_has_no_deploy_or_provision_command():
        result = subprocess.run(["python3", "-m", "smartpbx_agent_factory", "--help"], check=True, capture_output=True, text=True)
        assert "inspect" in result.stdout
        assert "open-pr" in result.stdout
        assert "deploy" not in result.stdout
        assert "provision" not in result.stdout

    def test_plan_does_not_write_target_repositories(tmp_path):
        result = invoke_cli(["plan", "--manifest", str(fixture_manifest_path()), "--state-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "wss_token" not in result.stdout

    def test_resume_refuses_changed_manifest_digest(tmp_path):
        generation_id = create_planned_generation(tmp_path)
        mutate_manifest()
        result = invoke_cli(["resume", generation_id, "--state-root", str(tmp_path)])
        assert result.exit_code == 3
        assert "manifest digest changed" in result.stderr

- [ ] **Step 2: Run the red tests.**

    python3 -B -m py_compile smartpbx_agent_factory/cli.py smartpbx_agent_factory/orchestrator.py smartpbx_agent_factory/gitops.py smartpbx_agent_factory/tests/test_cli.py smartpbx_agent_factory/tests/test_orchestrator.py smartpbx_agent_factory/tests/test_gitops.py

Expected: module entry point and orchestrator symbols are missing.

- [ ] **Step 3: Implement inspect, plan, generate, resume and abandon.**

inspect validates source repository, origin/main, Docker/GitHub CLI/SOPS/age binaries, operations repository, age recipients, provenance and dirty-tree overlap. For the website it rejects a dirty primary or colliding target, fetches the configured remote, pins the full `origin/main` SHA, and creates the isolated worktree without reset/clean. plan parses the manifest, validates the catalogue, derives resources, renders a redacted plan, computes manifest/knowledge/resource digests, and writes only mode-0600 state JSON. It asks for explicit knowledge and plan approvals.

generate creates three isolated worktrees from approved base revisions, reserves resources, resolves named secrets, builds approved knowledge, renders backend/website/operations artifacts, runs scans/tests, and transitions to VERIFIED only after all outputs pass. resume verifies all previous digests, reuses generated secret values, and resumes at the first incomplete stage. abandon deletes only generation-owned worktrees and plaintext paths; it never calls git clean/reset, Docker, Compose, SSH, DNS, TLS, or Client Connect APIs.

All Git, SOPS, age, Docker and gh calls use subprocess.run argument arrays with shell=False. The wrapper create-smartpbx-agent is an executable that runs python3 -m smartpbx_agent_factory.

- [ ] **Step 4: Verify GREEN and commit.**

    python3 -B -m py_compile smartpbx_agent_factory/cli.py smartpbx_agent_factory/orchestrator.py smartpbx_agent_factory/gitops.py
    python3 -m compileall -q smartpbx_agent_factory
    git diff --check
    git add smartpbx_agent_factory/cli.py smartpbx_agent_factory/__main__.py create-smartpbx-agent smartpbx_agent_factory/gitops.py smartpbx_agent_factory/orchestrator.py smartpbx_agent_factory/tests/test_cli.py smartpbx_agent_factory/tests/test_orchestrator.py smartpbx_agent_factory/tests/test_gitops.py
    git commit -m "feat(factory): add resumable isolated generation CLI"

Expected: syntax/static checks pass; CI behavioral status pending. Inspect/plan/abandon tests must not mutate target repositories.

### Task 9: Add disposable lifecycle verification and readiness report

**Files:**
- Create: smartpbx_agent_factory/verify.py
- Create: smartpbx_agent_factory/tests/test_verify.py
- Create: smartpbx_agent_factory/tests/fixtures/protocol_messages.json
- Modify: smartpbx_agent_factory/orchestrator.py

**Interfaces:**
- Produces: verify_generated_backend(agent_dir, resources) -> VerificationReport and readiness_report(report_set) -> str.
- The disposable client sends protocol-shaped connected/start/media/stop or hangup messages over a temporary loopback WebSocket, checks health, proves missing/wrong WSS credentials are rejected, and verifies clean teardown without transcript/audio output.

- [ ] **Step 1: Write red verification tests.**

    def test_readiness_report_contains_provenance_and_no_secret_values():
        report = readiness_report(fixture_report_set(secret_values=("marker",)))
        assert "template_version" in report
        assert "source_revision" in report
        assert "marker" not in report
        assert "transcript" not in report

    def test_contract_verifier_requires_agent_specific_ci_job(tmp_path):
        with pytest.raises(VerificationError, match="blocking CI"):
            verify_generated_backend(tmp_path, fixture_resources())

    def test_protocol_fixture_rejects_wrong_auth_before_start():
        result = run_disposable_client(fixture_backend(), header="wrong")
        assert result.close_code == 1008
        assert result.start_sent is False

- [ ] **Step 2: Run the red tests.**

    python3 -B -m py_compile smartpbx_agent_factory/verify.py smartpbx_agent_factory/tests/test_verify.py

Expected: missing verify functions or failed assertions.

- [ ] **Step 3: Implement build/import/health/WSS checks.**

Build the generated Dockerfile with a generation-scoped local tag, run the image with loopback-only ports, and execute generated /health and authenticated /smartpbx/status checks. Reject missing, wrong, cross-agent, non-constant-time, or wrong-header credentials; check account binding, bounded inputs, session admission, timeout recovery, interruption policy, privacy-safe diagnostics and clean resource teardown. Do not connect to a public hostname, use production credentials, or call the Client Connect dashboard.

The fixture client records only booleans and bounded counters: connected, start accepted, media accepted, invalid auth rejected, terminal event observed, active tasks after close and resource counts. It never records media bytes, transcript text, call IDs, caller numbers or provider responses.

- [ ] **Step 4: Verify GREEN and commit.**

    python3 -B -m py_compile smartpbx_agent_factory/verify.py smartpbx_agent_factory/tests/test_verify.py smartpbx_agent_factory/render.py
    git diff --check
    git add smartpbx_agent_factory/verify.py smartpbx_agent_factory/tests/test_verify.py smartpbx_agent_factory/tests/fixtures/protocol_messages.json smartpbx_agent_factory/orchestrator.py
    git commit -m "feat(factory): verify generated SmartPBX lifecycle"

Expected: syntax/static checks pass; CI behavioral status pending. Readiness output must be redacted.

### Task 10: Open three linked PRs and stop at the review boundary

**Files:**
- Create: smartpbx_agent_factory/prs.py
- Create: smartpbx_agent_factory/tests/test_prs.py
- Modify: smartpbx_agent_factory/orchestrator.py
- Modify: smartpbx_agent_factory/cli.py

**Interfaces:**
- Produces: PRSet(backend_url, operations_url, website_url, readiness_report_path, manifest_digest, artifact_digests) and the exact `PRProvider.open_pull_request`, `comment_pull_request`, and `update_pull_request_body` methods.
- Requires state stage VERIFIED and three clean generation-owned worktrees; refuses any missing readiness gate, secret scan, CI registration, or provenance record.

- [ ] **Step 1: Write red PR orchestration tests.**

    def test_pr_creation_requires_verified_state(fake_provider):
        with pytest.raises(StateError, match="VERIFIED"):
            open_linked_prs(fake_provider, fixture_state(stage=Stage.GENERATED))

    def test_three_prs_are_opened_sequentially_with_immutable_digests(fake_provider):
        result = open_linked_prs(fake_provider, fixture_state(stage=Stage.VERIFIED))
        assert result.backend_url
        assert result.operations_url
        assert result.website_url
        assert fake_provider.open_order == ["backend", "operations", "website"]
        assert all(len(digest) == 64 for digest in result.artifact_digests.values())
        assert fake_provider.comments[result.backend_url]
        assert fake_provider.comments[result.operations_url]
        assert fake_provider.comments[result.website_url]

    def test_pr_body_is_redacted(fake_provider):
        result = open_linked_prs(fake_provider, fixture_state(stage=Stage.VERIFIED, secret_values=("marker",)))
        assert "marker" not in fake_provider.bodies[result.backend_url]
        assert "wss://" in fake_provider.bodies[result.backend_url]

    def test_website_pr_is_review_only_until_routing_activation(fake_provider):
        result = open_linked_prs(fake_provider, fixture_state(stage=Stage.VERIFIED))
        assert "pending approved routing activation" in fake_provider.bodies[result.website_url]
        assert "backend health" in fake_provider.bodies[result.website_url]
        assert 'releaseState: "pending"' in fake_provider.bodies[result.website_url]
        assert "DEMO_AGENT_HOSTS" in fake_provider.bodies[result.website_url]
        assert fake_provider.release_allowed[result.website_url] is False

- [ ] **Step 2: Run the red tests.**

    python3 -B -m py_compile smartpbx_agent_factory/prs.py smartpbx_agent_factory/tests/test_prs.py

Expected: missing PR provider/orchestrator symbols.

- [ ] **Step 3: Implement the three-PR order.**

Open PRs strictly sequentially: (1) backend in the full-voice-agent worktree, (2) private operations in its existing private repository if and only if the SOPS/age prerequisites passed, and (3) website in the isolated `/home/dev/worktrees/taskforce-ai-website-<generation-id>` worktree. Each initial body contains the immutable full branch SHA, manifest digest, template/source revision and artifact digest, plus directional links only to already-known dependencies. The backend body cannot contain future URLs at creation time.

After the website URL is returned, call the exact `PRProvider.comment_pull_request(pull_request_url=..., body=...)` method for each open PR with the complete three-URL release order and all immutable digests. If the provider supports body replacement and the product requires the URLs in the body itself, use the exact `update_pull_request_body(pull_request_url=..., body=...)` method after all three URLs exist; otherwise retain the comments as the authoritative back-links. Tests must assert the call order and exact URL/digest contents, and that no invented `link_dependencies` API is used. The website body/comment must include `demo-routing-activation.md`, “pending approved routing activation,” and “backend health”; its review PR may exist but `release_allowed` remains false until a later separately approved action adds the shared `DEMO_AGENT_HOSTS` mapping and proves health/ingress. Task 10 does not perform that action. A failed later PR leaves earlier PRs intact, records the failure, and marks the generation BLOCKED; it never closes, force-updates, merges, or recreates a PR.

The open-pr command performs no merge, release, deployment, provisioning, DNS, TLS, host mutation or Client Connect dashboard mutation. It prints the three URLs and exact human review order, then stops.

- [ ] **Step 4: Verify GREEN and commit.**

    python3 -B -m py_compile smartpbx_agent_factory/prs.py smartpbx_agent_factory/tests/test_prs.py smartpbx_agent_factory/orchestrator.py
    git diff --check
    git add smartpbx_agent_factory/prs.py smartpbx_agent_factory/tests/test_prs.py smartpbx_agent_factory/orchestrator.py smartpbx_agent_factory/cli.py
    git commit -m "feat(factory): open linked review-only SmartPBX PRs"

Expected: syntax/static checks pass; CI behavioral status pending. Fake-provider CI tests must make no network, GitHub, deployment, or production mutation.

## Final verification and self-review

- [ ] Run the complete factory suite:

    python3 -B -m py_compile smartpbx_agent_factory/*.py smartpbx_agent_factory/tests/*.py
    python3 -m compileall -q smartpbx_agent_factory
    git diff --check

Expected: syntax/static checks pass; CI behavioral status pending. No syntax/diff errors occur.

- [ ] Run the website gate in its own repository:

    cd /home/dev/worktrees/taskforce-ai-website-<generation-id>
    npm run test:smartpbx
    npm run build

Expected: syntax/static checks pass; CI behavioral status pending. Website CI is the source of validator/build status.

- [ ] Inspect only the generated backend tree and verify:

    python3 -B -m py_compile "SmartPBX Agents/acme-inquiry"/*.py "SmartPBX Agents/acme-inquiry/tests"/*.py
    docker build -f "SmartPBX Agents/acme-inquiry/Dockerfile" -t smartpbx-acme-inquiry-ci "SmartPBX Agents/acme-inquiry"
    docker run --rm --entrypoint python smartpbx-acme-inquiry-ci -c "import server, smartpbx_gateway, smartpbx_protocol"

Expected: syntax/static checks pass; CI behavioral status pending. The Docker build/import commands are separately recorded only if actually run; this plan does not claim generated behavioral tests passed.

- [ ] Run the mandatory spec coverage audit. Confirm tasks cover:

  - provenance and frozen-template hashes (Tasks 3 and 6);
  - schema, catalogue, state, approvals and idempotency (Tasks 1, 2 and 8);
  - SOPS/age, secret redaction and encrypted operations PR (Task 4);
  - PDF/DOCX/TXT/Markdown and allowlisted URL ingestion (Task 5);
  - minimal backend, capabilities, infrastructure, Client Connect sheet and agent CI (Task 6);
  - website card, routing, language controls, fallback and backend-first dependency (Task 7);
  - disposable lifecycle, readiness report and privacy-safe diagnostics (Task 9);
  - three linked PRs and no provisioning/deployment (Task 10).

- [ ] Confirm the final self-review also records: website rendering persists only backend artifact/branch digests; HattonHills is the sole browser token issuer; generated agents omit `/api/voice-token` and token-minting Twilio credentials; `HattonHills/server.py` and `DEMO_AGENT_HOSTS` are untouched; `demo-routing-activation.md` is pending; `release_allowed` is false until later approved routing activation and backend health proof; and all local evidence is syntax/static only with CI behavioral status pending.

- [ ] Run the generated-tree identity and secret-pattern scan:

    rg -n -i 'Hatton Hills|TreeHouse|Mosvold|hotel_info|Yanolja|SMARTPBX_WS_TOKEN=|BEGIN[[:space:]]+PRIVATE[[:space:]]+KEY|api[-_ ]?key' smartpbx_agent_factory SmartPBX\ Agents

Expected: no client identity, production knowledge file, unresolved template marker, or plaintext secret. Approved documentation words in this plan are not generated output and are excluded from the scan's target tree.

## Safety stop conditions

Stop and mark the generation BLOCKED if provenance, recipient validation, knowledge approval, collision checks, identity scan, secret scan, build/import, WSS auth, agent CI registration, website build, or any PR creation prerequisite fails. Preserve redacted non-secret evidence and generation-owned worktrees for review. Never compensate with hot-copy, unguarded Compose recreation, production dashboard edits, DNS/TLS changes, or a guessed provider/API value.

## Execution handoff

Plan complete and saved to docs/superpowers/plans/2026-09-10-smartpbx-agent-factory.md. Terra must review this plan and the V07 plan before the parent dispatches the independent Luna lanes. After implementation, the parent performs the final Sol review and decides whether to merge; this plan does not authorize production provisioning or deployment.
