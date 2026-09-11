import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from smartpbx_agent_factory.orchestrator import (
    GenerationBlockedError,
    GenerationInfrastructureError,
    GenerationOrchestrator,
)
from smartpbx_agent_factory.gitops import WorktreeManager
from smartpbx_agent_factory.knowledge import (
    KnowledgeDocument,
    KnowledgeFact,
    KnowledgeReview,
    recompute_knowledge_review_digest,
)
from smartpbx_agent_factory.secrets import SecretAudit
from smartpbx_agent_factory.state import Stage


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


class FakeSecretProvider:
    """Exercise the sealed-bundle boundary without using real credentials."""

    def __init__(self, *, audit: SecretAudit | None = None) -> None:
        self.audit = audit
        self.validated = False
        self.fetched: list[str] = []
        self.generated: list[str] = []

    def validate(self) -> None:
        self.validated = True

    def fetch(self, name: str) -> str:
        self.fetched.append(name)
        return f"fixture-fetched-{len(self.fetched)}"

    def generate(self, name: str, *, length: int = 32) -> str:
        assert length == 32
        self.generated.append(name)
        return f"fixture-generated-{len(self.generated)}"

    def encrypt_yaml(self, plaintext: bytes, *, path: Path) -> bytes:
        assert plaintext and path.name.startswith(".sealed-")
        return b"sops:\n  age: encrypted"

    def audit_report(self) -> SecretAudit:
        return self.audit or SecretAudit(
            fetched_names=tuple(self.fetched), generated_names=tuple(self.generated)
        )


def test_plan_writes_only_private_state_without_claiming_secret_resolution(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    assert report.state.stage is Stage.INPUT_COLLECTED
    assert state_file.exists()
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert "wss_token" not in report.rendered_plan
    assert list(tmp_path.glob("*.json")) == [state_file]


def test_planned_transaction_loads_before_plan_approval_without_forging_approval_state(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)

    stored = orchestrator._load_verified(report.generation_id)

    assert stored.plan_digest == report.plan_digest
    assert stored.plan_artifact is not None
    assert stored.plan_artifact["digest"] == report.plan_digest
    assert stored.state.plan_digest is None
    assert stored.state.plan_approval_digest is None
    with pytest.raises(GenerationBlockedError, match="secret resolution"):
        orchestrator.resume(report.generation_id)


def test_cleanup_inventory_loads_current_sealed_ciphertext_schema():
    from smartpbx_agent_factory.orchestrator import _parse_cleanup_inventory

    inventory = _parse_cleanup_inventory(
        {
            "worktrees": [],
            "plaintext_paths": [],
            "completed_worktree_targets": [],
            "completed_plaintext_paths": [],
            "completed": False,
            "sealed_ciphertext_paths": [],
            "completed_sealed_ciphertext_paths": [],
        }
    )

    assert inventory is not None
    assert inventory.sealed_ciphertext_paths == ()
    assert inventory.completed_sealed_ciphertext_paths == ()


def test_plan_refuses_an_already_reserved_slug_and_resource_set(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    orchestrator.plan(FIXTURE)
    with pytest.raises(GenerationBlockedError, match="resource allocation conflict"):
        orchestrator.plan(FIXTURE)


def test_resume_refuses_changed_manifest_digest(tmp_path):
    fixture = tmp_path / "manifest.json"
    fixture.write_bytes(FIXTURE.read_bytes())
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(fixture)
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    raw["purpose"] = "Changed purpose"
    fixture.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GenerationBlockedError, match="manifest digest changed"):
        orchestrator.resume(report.generation_id)


def test_generate_fails_closed_before_any_artifact_without_both_approvals(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    with pytest.raises(GenerationBlockedError, match="secret resolution"):
        orchestrator.generate(report.generation_id)


def test_generate_partial_runtime_creates_no_worktree_or_factory_state_artifact(tmp_path):
    orchestrator = GenerationOrchestrator(
        tmp_path,
        catalogue_path=CATALOGUE,
        approved_source_roots=(FIXTURE.parents[3].resolve(),),
    )
    report = orchestrator.plan(FIXTURE)
    state = orchestrator.record_secrets_resolved(report.generation_id, provider=FakeSecretProvider())
    state = orchestrator.resume(report.generation_id, knowledge_approval=state.knowledge_review_digest)
    orchestrator.resume(report.generation_id, plan_approval=state.plan_digest)

    with pytest.raises(GenerationBlockedError, match="INCOMPLETE_TEMPLATE"):
        orchestrator.generate(report.generation_id)

    raw = json.loads((tmp_path / f"{report.generation_id}.json").read_text(encoding="utf-8"))
    assert raw["cleanup_inventory"]["worktrees"] == []
    assert not (tmp_path / "generated").exists()


def test_abandon_marks_generation_without_removing_state_audit(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state = orchestrator.abandon(report.generation_id)
    assert state.stage is Stage.ABANDONED
    assert (tmp_path / f"{report.generation_id}.json").exists()


def test_state_write_is_atomic_and_uses_replace(tmp_path, monkeypatch):
    replacements: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    report = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE).plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    assert len(replacements) == 1
    assert replacements[0][1] == state_file
    assert state_file.stat().st_mode & 0o777 == 0o600


def test_state_loader_rejects_symlinked_state_file(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    target = tmp_path / "other.json"
    target.write_bytes(state_file.read_bytes())
    state_file.unlink()
    state_file.symlink_to(target)
    with pytest.raises(GenerationInfrastructureError, match="symlink"):
        orchestrator.resume(report.generation_id)


def test_plan_rejects_knowledge_path_authorized_only_by_process_cwd(tmp_path):
    manifest = tmp_path / "manifest.json"
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["knowledge_sources"][0]["path"] = str(
        Path.cwd() / "smartpbx_agent_factory/tests/fixtures/acme-faq.txt"
    )
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GenerationBlockedError, match="approved root"):
        GenerationOrchestrator(tmp_path / "state", catalogue_path=CATALOGUE).plan(manifest)


def test_inspect_reports_explicit_provenance_repository_and_operations_gates(tmp_path):
    report = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE).inspect(FIXTURE)
    checks = report["checks"]
    assert {"source_repository", "origin_main", "template_provenance", "operations_prerequisites"} <= set(checks)
    assert checks["template_provenance"].startswith(("ready", "blocked:"))
    assert checks["operations_prerequisites"].startswith(("ready", "blocked:"))


def test_abandon_refuses_legacy_state_without_cleanup_inventory(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    raw.pop("cleanup_inventory", None)
    state_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(GenerationBlockedError, match="cleanup inventory"):
        orchestrator.abandon(report.generation_id)


def test_state_root_rejects_a_symlinked_parent_before_writing(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(GenerationInfrastructureError, match="symlink"):
        GenerationOrchestrator(linked_parent / "state", catalogue_path=CATALOGUE).plan(FIXTURE)


def test_resume_rejects_state_file_with_non_private_mode(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    state_file = tmp_path / f"{report.generation_id}.json"
    state_file.chmod(0o644)
    with pytest.raises(GenerationInfrastructureError, match="mode 0600"):
        GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE).resume(report.generation_id)


def test_abandon_can_recover_a_recorded_worktree_after_orchestrator_restart(tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    worktree_root = tmp_path / "worktrees"
    target = worktree_root / "agent"
    removed: list[tuple[str, ...]] = []

    def run(args):
        if args[-2:] == ("status", "--porcelain"):
            return ""
        if args[-1] == "origin/main":
            return "a" * 40
        if args[-2:] == ("list", "--porcelain"):
            return f"worktree {primary}\nHEAD {'b' * 40}\n\nworktree {target}\nHEAD {'a' * 40}\ndetached\n\n"
        if "remove" in args:
            removed.append(tuple(args))
        return ""

    manager = WorktreeManager(worktree_root, run=run)
    handle = manager.create(primary=primary, remote="origin", revision="a" * 40, target=target)
    factory = lambda temporary_root: WorktreeManager(temporary_root, run=run)
    first = GenerationOrchestrator(
        tmp_path / "state", catalogue_path=CATALOGUE, worktree_manager_factory=factory
    )
    report = first.plan(FIXTURE)
    first.record_owned_worktree(report.generation_id, manager, handle)
    restarted = GenerationOrchestrator(
        tmp_path / "state", catalogue_path=CATALOGUE, worktree_manager_factory=factory
    )
    restarted.abandon(report.generation_id)
    assert removed


def test_abandon_persists_each_completed_cleanup_item_before_the_next_failure(tmp_path, monkeypatch):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    first = tmp_path / "plaintext" / report.generation_id / "first"
    second = tmp_path / "plaintext" / report.generation_id / "second"
    first.parent.mkdir(parents=True)
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    orchestrator.record_owned_plaintext_path(report.generation_id, first)
    orchestrator.record_owned_plaintext_path(report.generation_id, second)
    original_unlink = Path.unlink

    def fail_second(path, *args, **kwargs):
        if path == second:
            raise OSError("simulated second cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)
    with pytest.raises(OSError, match="second cleanup failure"):
        orchestrator.abandon(report.generation_id)
    raw = json.loads((tmp_path / f"{report.generation_id}.json").read_text(encoding="utf-8"))
    assert str(first) in raw["cleanup_inventory"]["completed_plaintext_paths"]
    assert str(second) not in raw["cleanup_inventory"]["completed_plaintext_paths"]


def test_plaintext_registration_persists_owned_path_without_constructor_error(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    path = tmp_path / "plaintext" / report.generation_id / "value"
    path.parent.mkdir(parents=True)
    path.write_text("temporary", encoding="utf-8")
    orchestrator.record_owned_plaintext_path(report.generation_id, path)
    raw = json.loads((tmp_path / f"{report.generation_id}.json").read_text(encoding="utf-8"))
    assert raw["cleanup_inventory"]["plaintext_paths"] == [str(path)]


def test_secret_resolution_requires_a_validated_provider_audit_and_binds_digest(tmp_path):
    provider = FakeSecretProvider()
    orchestrator = GenerationOrchestrator(
        tmp_path,
        catalogue_path=CATALOGUE,
        approved_source_roots=(FIXTURE.parents[3].resolve(),),
    )
    report = orchestrator.plan(FIXTURE)
    state = orchestrator.record_secrets_resolved(
        report.generation_id,
        provider=provider,
    )
    assert provider.validated is True
    assert state.stage is Stage.KNOWLEDGE_REVIEW_REQUIRED
    assert len(state.stage_digests["secrets"]) == 64


def test_secret_resolution_rejects_ciphertext_that_contains_an_exact_plaintext_value(tmp_path):
    class LeakingProvider(FakeSecretProvider):
        def encrypt_yaml(self, plaintext: bytes, *, path: Path) -> bytes:
            assert plaintext and path.name.startswith(".sealed-")
            return b"sops:\n" + plaintext

    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)

    with pytest.raises(GenerationBlockedError, match="plaintext secret"):
        orchestrator.record_secrets_resolved(report.generation_id, provider=LeakingProvider())

    assert not (tmp_path / "sealed-secrets" / report.generation_id).exists()


@pytest.mark.parametrize(
    "audit",
    (
        SecretAudit(generated_names=()),
        SecretAudit(generated_names=("acme-inquiry/wss_token", "other/wss_token")),
    ),
)
def test_secret_resolution_rejects_missing_or_extra_provider_audit_names(tmp_path, audit):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    with pytest.raises(GenerationBlockedError, match="secret audit names"):
        orchestrator.record_secrets_resolved(report.generation_id, provider=FakeSecretProvider(audit=audit))


def test_secret_resolution_does_not_accept_a_caller_forged_audit_argument(tmp_path):
    orchestrator = GenerationOrchestrator(tmp_path, catalogue_path=CATALOGUE)
    report = orchestrator.plan(FIXTURE)
    with pytest.raises(TypeError):
        orchestrator.record_secrets_resolved(
            report.generation_id,
            provider=FakeSecretProvider(),
            audit=SecretAudit(generated_names=("forged/wss_token",)),
        )


def test_secret_resolution_binds_the_concrete_knowledge_review_not_manifest_source_metadata(tmp_path):
    class Builder:
        def __init__(self):
            self.calls = []
            self.review_digest = ""

        def build(self, sources, output_dir):
            self.calls.append((sources, output_dir))
            output_dir.mkdir(parents=True, exist_ok=True)
            review = KnowledgeReview(
                facts=(KnowledgeFact("Approved local fact.", "file:///fixture", "document"),),
                conflicts=(),
                missing_facts=(),
                sensitive_findings=(),
                inaccessible_sources=(),
                duplicate_facts=(),
                instruction_findings=(),
                digest="",
                documents=(KnowledgeDocument("file:///fixture", "owner", "2026-01-01", "public", "Approved local fact."),),
            )
            self.review_digest = recompute_knowledge_review_digest(review)
            return replace(review, digest=self.review_digest)

    builder = Builder()
    orchestrator = GenerationOrchestrator(
        tmp_path,
        catalogue_path=CATALOGUE,
        knowledge_builder_factory=lambda _root: builder,
    )
    report = orchestrator.plan(FIXTURE)
    state = orchestrator.record_secrets_resolved(report.generation_id, provider=FakeSecretProvider())

    assert state.stage is Stage.KNOWLEDGE_REVIEW_REQUIRED
    assert state.knowledge_review_digest == builder.review_digest
    assert builder.calls and builder.calls[0][0]
    assert builder.calls[0][1] == tmp_path / "knowledge-reviews" / report.generation_id
    assert (tmp_path / "knowledge-reviews").stat().st_mode & 0o777 == 0o700
    assert builder.calls[0][1].stat().st_mode & 0o777 == 0o700
    persisted = json.loads((tmp_path / f"{report.generation_id}.json").read_text(encoding="utf-8"))
    assert persisted["knowledge_review"]["digest"] == builder.review_digest
    assert persisted["knowledge_review"]["documents"][0]["text"] == "Approved local fact."


def test_secret_resolution_rejects_a_noncanonical_knowledge_review_digest(tmp_path):
    class Builder:
        def build(self, sources, output_dir):
            return KnowledgeReview(
                facts=(), conflicts=(), missing_facts=(), sensitive_findings=(),
                inaccessible_sources=(), duplicate_facts=(), instruction_findings=(),
                digest="a" * 64,
            )

    orchestrator = GenerationOrchestrator(
        tmp_path,
        catalogue_path=CATALOGUE,
        knowledge_builder_factory=lambda _root: Builder(),
    )
    report = orchestrator.plan(FIXTURE)
    with pytest.raises(GenerationBlockedError, match="knowledge review digest"):
        orchestrator.record_secrets_resolved(report.generation_id, provider=FakeSecretProvider())
