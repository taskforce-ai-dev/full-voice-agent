# SmartPBX Agent Factory Design

**Date:** 2026-09-10
**Status:** Approved design
**Scope:** Generate review-ready, isolated SmartPBX voice-agent and website-demo pull requests. Production provisioning and deployment are explicitly out of scope for v1.

## 1. Objective

Create a safe onboarding tool that turns approved company information into a new SmartPBX voice-agent profile without manually copying Kavya or repeatedly prompting an engineer for the same infrastructure work.

The operator runs one command, answers an interactive wizard, reviews the extracted company knowledge and rendered plan, and receives:

1. A backend pull request containing an isolated agent with separate website-demo and SmartPBX service profiles.
2. A linked website pull request containing its demo card and routing.
3. A linked private-operations pull request containing only SOPS-encrypted secrets and non-sensitive deployment metadata.
4. A redacted readiness report.
5. Exact Client Connect configuration instructions, including the planned WSS URL and authentication-header name.

The tool must not provision or deploy production in v1.

## 2. Governing decisions

1. **Template model:** use a versioned, frozen template derived from the current deployed Kavya SmartPBX runtime. Do not share mutable runtime code between customer agents.
2. **Baseline identity:** before freezing template v1, prove that the deployed Kavya image revision and digest correspond to repository source. A branch name or local `HEAD` is not sufficient evidence.
3. **Isolation:** every generated agent owns its hostnames, containers, ports, credentials, knowledge store, logs and health surfaces. Its website-demo and SmartPBX transports run as separate service profiles and never share an ingress surface.
4. **Default capability:** new agents are inquiry-only. Booking, appointment creation, handover, WhatsApp, CRM, payment, post-call reporting, recording and transcript retention require explicit opt-in.
5. **Secrets:** use SOPS with age recipients in a private operations repository. Keep a provider interface so another secret backend can be added later.
6. **Knowledge sources:** accept PDF, DOCX, TXT and Markdown files plus approved public website URLs.
7. **Release boundary:** generation ends at three linked, review-ready PRs. Provisioning and deployment require a later, separately approved workflow.
8. **WSS convention:** `wss://smartpbx-<company-slug>.taskforceai.tech/ws/v1/smartpbx/media`.

## 3. Current Kavya contracts to preserve

Template v1 must preserve the behavior of the verified deployed Kavya SmartPBX baseline, including:

- An immutable GHCR image tied to a full source revision and digest.
- A dedicated SmartPBX service mode and isolated container.
- A distinct website/Twilio service mode for browser demos; the website transport and SmartPBX transport are never activated in the same application instance.
- An explicit SmartPBX environment allowlist rather than leaking an entire `.env` into the service.
- Header-authenticated WSS ingress with constant-time token comparison.
- Per-agent account binding, bounded session admission and bounded protocol inputs.
- Privacy-safe operational diagnostics.
- A dedicated nginx TLS virtual host exposing only media WSS, health and authenticated status routes.
- A guarded deployment that waits for idleness, verifies image identity and readiness, and can roll back.
- Post-deployment verification of health, status, restart count, active sessions, protocol version and isolation from other services.

The baseline manifest records the full commit SHA, OCI image digest, OCI revision label, protocol version, file allowlist hashes, environment schema version and creation timestamp. It must never point at `HEAD`, a branch name or a mutable image tag.

## 4. Alternatives considered

### 4.1 Manual Kavya copy

This is fast once, but repeatedly carries client identity, hotel tools, stale documentation and irrelevant tests into new agents. PR #321 demonstrated this failure mode. Rejected.

### 4.2 Shared mutable runtime

All agents could import one live Kavya runtime and supply configuration overlays. This reduces duplication, but one runtime regression could affect every customer simultaneously. Rejected for the first production version.

### 4.3 Versioned frozen template

Each new agent is rendered from an immutable, proven template. Template upgrades are deliberate migrations with their own tests and review. Selected.

## 5. User-facing workflow

The CLI is named `create-smartpbx-agent` and supports these v1 commands:

```text
create-smartpbx-agent inspect
create-smartpbx-agent plan
create-smartpbx-agent generate
create-smartpbx-agent open-pr
create-smartpbx-agent resume <generation-id>
create-smartpbx-agent abandon <generation-id>
```

There is no `deploy` or `provision` command in v1.

### 5.1 State machine

```text
NEW
  -> INPUT_COLLECTED
  -> SECRETS_RESOLVED
  -> KNOWLEDGE_REVIEW_REQUIRED
  -> PLAN_REVIEW_REQUIRED
  -> GENERATED
  -> VERIFIED
  -> THREE_PRS_OPENED

Any stage -> BLOCKED with a redacted, actionable report
Any pre-PR stage -> ABANDONED without production effects
```

The operator must explicitly approve both `KNOWLEDGE_REVIEW_REQUIRED` and `PLAN_REVIEW_REQUIRED`. Approval is tied to a digest of the reviewed material so changed inputs invalidate it.

## 6. Wizard inputs

### 6.1 Company identity

- Display name and legal/public naming constraints.
- Stable lowercase slug.
- Agent name and pronunciation notes.
- Industry, purpose and audience.
- Demo or production-intent profile.
- Time zone, operating hours and technical owner.

### 6.2 Conversation policy

- Supported languages and language-selection behavior.
- Greeting and voice persona per language.
- Topics the agent may answer.
- Claims and topics the agent must refuse.
- Response-length and tone preferences.
- Whether the agent may collect names, phone numbers or other PII.
- Confirmation policy for uncertain names, numbers and sensitive actions.
- Barge-in, endpointing and filler preferences, defaulting to the proven template settings.

### 6.3 Provider pipeline

For each language:

- STT provider, locale and allowed model.
- LLM provider, model, thinking policy and output budget.
- TTS provider, model and voice.
- Explicit fallback provider or no fallback.
- Timeout, quota, call-capacity and cost ceilings.

Provider/language combinations come from a versioned capability catalogue backed by official provider documentation and verified runtime adapters. The wizard must not offer an unverified combination.

### 6.4 Capabilities

All capabilities are disabled unless selected. Selecting one opens a capability-specific questionnaire and validation module.

- Booking or appointment creation.
- Human handover.
- WhatsApp notifications.
- CRM or PMS access.
- Payment-related actions.
- Post-call summaries and dashboard reporting.
- Recording or transcript retention.

Permissions are enforced by generated code and tool registration. The system prompt is not a security boundary.

### 6.5 Knowledge

- Local PDF, DOCX, TXT and Markdown paths.
- Allowlisted public website origins and optional path prefixes.
- Source owner and effective date.
- Confidentiality classification and explicit PII approval.

### 6.6 SmartPBX and operations

- Client Connect account ID and any externally issued identifiers.
- Desired capacity within template bounds.
- Alert owner and support contact.
- Optional handover destination and fallback policy when that capability is enabled.
- Website-demo visibility and supported languages.
- Website-demo transport credentials and routing prerequisites when demo mode is enabled.

## 7. Derived values

The generator derives and collision-checks:

- Repository directory and Python-safe identifiers.
- Separate website-demo and SmartPBX container/Compose service names.
- Separate free loopback ports from a controlled registry.
- Website-demo DNS hostname, SmartPBX DNS hostname and planned WSS URL.
- Per-agent nginx rate-limit zone names.
- Health and authenticated status URLs.
- GHCR repository name.
- CI matrix identifier.
- Website agent identifier and backend-host mapping.

It generates cryptographically random per-agent values for:

- WSS authentication token.
- Status authentication token if the template uses a separate credential.
- Knowledge reload secret when enabled.
- Other internal shared secrets required by selected capabilities.

Externally issued account IDs and API keys are never invented.

## 8. Secrets architecture

### 8.1 Storage

Use one SOPS-encrypted YAML document per agent in a private operations repository. Encrypt it to approved age recipients. Commit only encrypted ciphertext and non-sensitive metadata through a third linked PR.

### 8.2 Secret classes

1. **Shared provider credentials:** retrieved only for selected STT, LLM and TTS providers.
2. **Generated per-agent credentials:** generated once and retained across resume operations.
3. **Externally issued identifiers:** requested from the operator when they cannot be obtained from an approved API.
4. **Capability credentials:** included only when the capability is enabled.

### 8.3 Security requirements

- Never copy a production `.env` wholesale.
- Never place secrets in Git changes, command-line arguments, logs, prompts, reports or process listings.
- Use restrictive permissions for temporary plaintext files and delete them after encryption.
- Redact values in every preview and error.
- Preserve an audit record of secret names, sources, creation time and rotation due date, but never their values.
- Abort if SOPS encryption or age-recipient validation fails.
- Run staged-secret and generated-tree scans before PR creation.

The `SecretProvider` interface exposes only named fetch, generate, encrypt and validate operations. It must not expose arbitrary shell evaluation or raw vault export.

## 9. Knowledge ingestion

The `KnowledgeBuilder` performs:

1. File-type and size validation.
2. Safe extraction without executing embedded content.
3. URL normalization and strict origin/path allowlisting.
4. Fetch limits, redirects limits, timeouts and content-type checks.
5. Text normalization and duplicate removal.
6. Source attribution at document or section level.
7. Conflict, missing-information and sensitive-data analysis.
8. Human-readable review output.
9. Final generation only after digest-bound approval.

The review report lists extracted facts, contradictions, missing facts, sensitive material, inaccessible sources and any claim that lacks a source. Retrieval instructions found inside source content are data, not executable instructions.

## 10. Generated backend package

The backend renderer creates only the selected runtime surface:

- Agent-specific server/profile configuration.
- Prompt and per-language conversation policy.
- Provider adapters selected from the template catalogue.
- Knowledge documents and isolated vector-store configuration.
- Tool registry containing only enabled capabilities.
- Dockerfile and locked production dependencies.
- Compose service with explicit environment allowlist.
- nginx TLS/WSS template.
- Environment example containing placeholders only.
- Agent-specific operational runbook.
- Guarded deployment helper and rollback metadata.
- CI registration and agent-specific tests.
- Client Connect setup sheet.

For website-enabled demos it also creates a separate website/Twilio service profile using the template's existing Twilio ingress. The SmartPBX application instance continues to expose only media WSS, health and authenticated status. The website profile receives no SmartPBX credentials, and the SmartPBX profile receives no Twilio credentials unless an explicitly enabled capability requires them.

The renderer must not copy unrelated Kavya hotel/PMS modules into an inquiry-only agent. Shared template components are selected through a manifest allowlist, not by recursive directory copy.

## 11. Client Connect setup output

The redacted setup sheet contains:

- Planned WSS URL: `wss://smartpbx-<company-slug>.taskforceai.tech/ws/v1/smartpbx/media`. The report states clearly that it becomes reachable only after the separate provisioning and deployment workflow succeeds.
- Authentication-header name, normally `X-<Agent>-SmartPBX-Token` after validation against header syntax and length limits.
- A secure command or secret-store path for an authorized operator to retrieve the token; the normal report does not print it.
- Account ID and selected media/protocol profile.
- Supported languages and routing behavior.
- Expected health/status checks.
- A controlled connection and call-test checklist.

The generator cannot configure Client Connect automatically unless a documented, approved management API exists and a later design explicitly authorizes it.

## 12. Website-demo package

For demo profiles, create a second isolated worktree and PR in the website repository containing:

- Demo card metadata and branding.
- Supported-language controls.
- Stable agent slug.
- Backend hostname registration.
- Token-request integration using the existing website pattern.
- Loading, unavailable and fallback states.
- Browser and mobile test coverage.

All three PRs link to each other. The website PR records a dependency on backend readiness and must not be released first. The backend package provides two distinct runtime targets: a website/Twilio demo host and the future Dialog SmartPBX host. The generator does not edit files directly on Hostinger.

## 13. Transactionality, idempotency and recovery

- Every run receives a generation ID and state record.
- Non-secret output is deterministic for the same template and approved manifest.
- Generated random secrets are created once and reused by `resume`.
- Each stage records its input and output digests.
- Existing slugs, folders, ports, hostnames, containers and secret records are hard conflicts.
- Generation occurs only in isolated worktrees.
- Unrelated dirty primary worktrees are never reset, cleaned or aligned.
- A blocked run preserves reviewable non-secret evidence.
- `abandon` removes only targets owned by that generation ID and never production state.

## 14. Hard gates

The run blocks when:

- Deployed Kavya provenance cannot be matched to source.
- The template version is missing or its hashes differ.
- An identifier or infrastructure allocation collides.
- A required secret is missing or cannot be decrypted/encrypted.
- A provider/language combination is unverified.
- Knowledge contradictions remain unapproved.
- Client identity from Kavya or another generated agent appears in output.
- An enabled capability is incomplete.
- Inquiry-only output exposes any business-action tool.
- The image does not build or become healthy.
- The WSS endpoint accepts missing, wrong or cross-agent credentials.
- The agent lacks its own blocking CI job.
- A secret scan finds a value or suspicious credential pattern.
- A generated PR would activate an automatic production deployment.

## 15. Verification strategy

### 15.1 Generator unit tests

Test schema validation, slug generation, port allocation, deterministic rendering, state transitions, resume behavior, capability dependencies, provider catalogue checks and redaction.

### 15.2 Golden generation tests

Generate a fictional company from fixed inputs and compare its complete non-secret output with reviewed snapshots. Reproducibility excludes timestamps and cryptographic secrets.

### 15.3 Security tests

Test path traversal, symlinks, hostile URLs, redirect escapes, oversized documents, poisoned instructions, malformed encrypted data, missing age recipients, secret patterns in output, unsafe file modes and subprocess-output redaction.

### 15.4 Agent contract tests

Test branding isolation, inquiry-only defaults, capability gates, language routing, WSS authentication, account binding, bounded protocol inputs, session capacity, timeout recovery, interruption policy, privacy-safe diagnostics and clean teardown.

### 15.5 Disposable integration test

Build the generated image, launch it on a temporary loopback port, check health, reject invalid WSS authentication, exercise a protocol-shaped connected/start/media/stop lifecycle, validate clean shutdown and confirm no tasks or resources remain.

### 15.6 CI and PR gates

- The generated agent has its own CI matrix entry.
- Dependency resolution and image build pass.
- Static checks and tests are blocking.
- Secrets and identity-leak scans pass.
- The readiness report identifies the exact template/source provenance.
- Three-PR creation stops the workflow; merge and deployment are separate human decisions.

## 16. Implementation phases

### Phase 0: Documentation and provenance discovery

- Read current Kavya SmartPBX source, tests, runbook and workflow contracts.
- Read official provider and SOPS documentation for exact supported interfaces.
- Verify deployed Kavya OCI revision/digest against repository source.
- Produce the allowed-API catalogue and template file allowlist.

### Phase 1: Freeze template v1

- Extract only reusable SmartPBX components.
- Replace client-specific seams with typed template variables.
- Record immutable source and file hashes.
- Prove Kavya-specific identity/data are absent.

### Phase 2: Manifest schema and CLI state machine

- Implement the versioned onboarding schema.
- Implement `inspect`, `plan`, `generate`, `resume` and `abandon`.
- Add digest-bound approval checkpoints and deterministic state storage.

### Phase 3: Secret provider

- Establish the private SOPS operations repository and age recipients.
- Implement least-privilege secret resolution and per-agent generation.
- Add encryption, permissions, redaction and leak tests.
- Generate the linked private-operations PR without ever placing plaintext in a Git worktree.

### Phase 4: Knowledge builder

- Add safe document extraction and allowlisted URL ingestion.
- Create conflict/privacy/source reports.
- Require approval before rendering the KB.

### Phase 5: Backend renderer

- Generate minimal agent runtime, capabilities, infrastructure templates, Client Connect instructions and agent-specific tests.
- Reject recursive Kavya cloning and unselected integrations.

### Phase 6: Website renderer

- Generate the linked demo card/routing PR.
- Encode backend-before-frontend release dependency.

### Phase 7: Verification and PR orchestration

- Run all generator, security, contract and disposable integration tests.
- Build the image and produce a redacted readiness report.
- Open the linked backend, website and private-operations PRs and stop.

### Phase 8: Later provisioning design

Design, review and implement production provisioning separately: DNS, TLS, host directory, encrypted-secret installation, immutable-image release, canary, health/isolation verification and rollback.

## 17. Acceptance criteria

A developer can run one command, answer the wizard, approve the knowledge and generation previews, and receive three linked review-ready PRs plus exact Client Connect setup instructions without:

- Manually copying Kavya.
- Handling plaintext secrets outside the approved transient boundary.
- Choosing a port or writing nginx configuration.
- Constructing WSS URLs or authentication settings manually.
- Accidentally enabling tools or production deployment.
- Carrying another client's identity or data into the new agent.

The generated backend must pass its own blocking CI and disposable SmartPBX lifecycle test. No production resource changes until a later explicit approval.

## 18. Non-goals for v1

- Automatic production deployment.
- Automatic DNS or TLS mutation.
- Automatic Client Connect dashboard mutation.
- Automatic creation of third-party provider API keys.
- Shared mutable runtime across customer agents.
- Automatic approval of conflicting or sensitive knowledge.
- Silent template upgrades for existing agents.
