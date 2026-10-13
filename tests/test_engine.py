# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for change sets and the change engine (fake compiler and deployment runner, real git)."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from citadel_agent.azure_clients.bicep import CompiledDeployment
from citadel_agent.azure_clients.credentials import PrincipalInfo
from citadel_agent.azure_clients.deployments import DeploymentRecord, WhatIfChange, WhatIfReport
from citadel_agent.changes.changeset import ChangeSet, ChangeStep, StepKind
from citadel_agent.changes.engine import (
    ChangeEngine,
    ChangeError,
    ChangeRequest,
    EngineServices,
    IntentChange,
    PrerequisiteStep,
)
from citadel_agent.changes.git_providers import PullRequestInfo
from citadel_agent.changes.repo_init import RepoInitError, allow_recording_pushes, init_canonical_repository
from citadel_agent.changes.store import ChangeStore
from citadel_agent.changes.workspace import CanonicalWorkspace, WorkspaceError, is_local_location
from citadel_agent.contracts import parse_intent
from citadel_agent.policy import Authorizer, Caller, Operation, Persona, load_admin_policy
from citadel_agent.proc import run_command
from citadel_agent.safety.audit import AuditLog
from citadel_agent.upstream.lock import UpstreamLock, UpstreamPin, read_release

SOURCE_UPSTREAM = Path(__file__).resolve().parents[1] / "upstream" / "ai-hub-gateway"
SUB = "11111111-1111-1111-1111-111111111111"
USER = PrincipalInfo(object_id="oid-1", tenant_id="t", display_name="dev@contoso.com", principal_type="User")
APIM_KEY = "0123456789abcdef0123456789abcdef"
APIM_ID = f"/subscriptions/{SUB}/resourceGroups/rg-dev/providers/Microsoft.ApiManagement/service/apim-dev"

POLICY = f"""
version: 1
defaultEnvironment: dev
environments:
  dev:
    class: dev
    mode: direct
    fallbackToPr: true
    subscriptionId: {SUB}
    location: swedencentral
    tenantId: 22222222-2222-2222-2222-222222222222
    apim: {{subscriptionId: {SUB}, resourceGroupName: rg-dev, name: apim-dev}}
    gatewayUrl: https://apim-dev.azure-api.net
    apimManagedIdentity:
      {{subscriptionId: {SUB}, resourceGroupName: rg-dev, name: id-apim-dev, clientId: {SUB}}}
    keyVault: {{subscriptionId: {SUB}, resourceGroupName: rg-dev, name: kv-dev}}
    appInsights: {{subscriptionId: {SUB}, resourceGroupName: rg-dev, name: appi-dev}}
    caps:
      maxTokensPerMinute: 50000
gitops:
  repository: contoso/citadel-contracts
"""


def git(cwd: Path, *args: str) -> str:
    return run_command("git", args, cwd=cwd).stdout.strip()


def _init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")


@pytest.fixture(scope="session")
def seed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not (SOURCE_UPSTREAM / "release.json").is_file():
        pytest.skip("Upstream submodule is not checked out")
    base = tmp_path_factory.mktemp("engine")
    upstream = base / "upstream-src"
    _init(upstream)
    for folder in (
        "citadel-access-contracts",
        "llm-backend-onboarding",
        "citadel-publish-contracts",
        "app-insights-alert",
    ):
        shutil.copytree(SOURCE_UPSTREAM / "bicep" / "infra" / folder, upstream / "bicep" / "infra" / folder)
    shutil.copy2(SOURCE_UPSTREAM / "release.json", upstream / "release.json")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "upstream")

    repo = base / "seed-repo"
    _init(repo)
    git(
        repo, "-c", "protocol.file.allow=always", "submodule", "add", str(upstream), "upstream/ai-hub-gateway"
    )
    (repo / ".citadel").mkdir()
    (repo / ".citadel" / "admin-policy.yaml").write_text(POLICY, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "seed")
    return repo


class FakeRunner:
    def __init__(self) -> None:
        self.changes = [
            (
                "Create",
                f"/subscriptions/{SUB}/resourceGroups/rg-dev/providers/Microsoft.ApiManagement/service/apim-dev/products/p",
            )
        ]
        self.what_if_calls: list[str] = []
        self.deploy_calls: list[str] = []

    def what_if(self, target: Any, name: str, compiled: CompiledDeployment) -> WhatIfReport:
        self.what_if_calls.append(name)
        return WhatIfReport(
            status="Succeeded",
            changes=tuple(WhatIfChange(resource_id=r, change_type=t) for t, r in self.changes),
        )

    def deploy(self, target: Any, name: str, compiled: CompiledDeployment) -> DeploymentRecord:
        self.deploy_calls.append(name)
        return DeploymentRecord(
            name=name,
            provisioning_state="Succeeded",
            outputs={
                "activeKeySlot": {"value": "primary"},
                "endpoints": {"value": [{"apiKey": APIM_KEY}]},
                "subscriptions": {"value": [{"name": "LLM-HR-Chat-DEV-SUB-01"}]},
            },
        )


def fake_compile(params_file: Path) -> CompiledDeployment:
    text = params_file.read_text(encoding="utf-8")
    return CompiledDeployment(
        params_file=params_file,
        template_file=params_file,
        template={"t": 1},
        parameters={"text": {"value": text}},
    )


class Harness:
    def __init__(
        self,
        repo: Path,
        tmp: Path,
        *,
        persona: Persona = Persona.OPERATOR,
        can_deploy: bool = True,
        workspace: CanonicalWorkspace | None = None,
        git_provider: Any = None,
    ) -> None:
        self.repo = repo
        self.runner = FakeRunner()
        self.release = read_release(repo / "upstream" / "ai-hub-gateway")
        self.live: dict[str, str] | None = dict(self.release)
        loaded = load_admin_policy(repo)
        assert loaded.writable, loaded.error
        self.workspace = workspace or CanonicalWorkspace(repo)
        self.store = ChangeStore(tmp / "changes")
        self.audit_dir = tmp / "audit"
        self.engine = ChangeEngine(
            EngineServices(
                workspace=self.workspace,
                store=self.store,
                authorizer=Authorizer(loaded, Caller(USER, persona)),
                agent_lock=UpstreamLock(
                    upstream=UpstreamPin(
                        repository="r", branch="b", commit="a" * 40, path="p", release=self.release
                    ),
                    synced_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                agent_version="0.1.0",
                runner_factory=lambda sub: self.runner,
                live_release=lambda env: self.live,
                can_deploy=lambda env: can_deploy,
                audit=AuditLog(self.audit_dir),
                git_provider=git_provider,
                compile=fake_compile,
            )
        )


def access(owners: list[str] | None = None, tpm: int = 5000, name: str = "Chat") -> Any:
    return parse_intent(
        {
            "kind": "access-contract",
            "environment": "dev",
            "metadata": {"owners": owners if owners is not None else ["dev@contoso.com"]},
            "useCase": {"businessUnit": "HR", "useCaseName": name, "environment": "DEV"},
            "services": [
                {
                    "code": "LLM",
                    "apis": ["universal-llm-api"],
                    "keySecretName": f"HR-{name.upper()}-KEY",
                    "policy": {"allowedModels": ["gpt-5.2"], "tokenLimit": {"tokensPerMinute": tpm}},
                }
            ],
        }
    )


@pytest.fixture
def harness(seed: Path, tmp_path: Path) -> Harness:
    repo = tmp_path / "repo"
    shutil.copytree(seed, repo)
    return Harness(repo, tmp_path)


def request(*changes: IntentChange, prerequisites: tuple[PrerequisiteStep, ...] = ()) -> ChangeRequest:
    return ChangeRequest(
        summary="Onboard HR chat", environment="dev", changes=changes, prerequisites=prerequisites
    )


def test_change_set_dag_validation() -> None:
    base = {
        "id": "cs-20261007-120000-abcdef",
        "createdAt": "2026-10-07T12:00:00Z",
        "createdBy": "x",
        "environment": "dev",
        "environmentClass": "dev",
        "mode": "pr",
        "summary": "s",
        "intents": [],
        "files": {},
        "repoBaseCommit": "c",
        "compat": {"agentVersion": "1", "agentUpstreamCommit": "a", "repoUpstreamCommit": "b"},
        "branch": "b",
    }
    step = {"kind": "prerequisite", "description": "d"}
    with pytest.raises(ValueError, match="cycle"):
        ChangeSet.model_validate(
            {
                **base,
                "steps": [{**step, "id": "a", "dependsOn": ["b"]}, {**step, "id": "b", "dependsOn": ["a"]}],
            }
        )
    with pytest.raises(ValueError, match="missing"):
        ChangeStep(id="x", kind=StepKind.DEPLOY, description="d")


def test_direct_change_end_to_end(harness: Harness) -> None:
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    change = prepared.change
    assert change.mode == "direct"
    assert change.steps[0].what_if is not None and change.steps[0].what_if.counts == {"Create": 1}
    assert "Approval hash" in prepared.summary()

    with pytest.raises(ChangeError, match="content hash"):
        harness.engine.execute(change.id, "sha256:wrong")
    assert harness.runner.deploy_calls == []

    result = harness.engine.execute(change.id, prepared.content_hash)
    assert harness.runner.deploy_calls == [change.steps[0].deployment_name]
    assert result["mode"] == "direct"
    assert result["recorded"] == "local-branch"
    status = harness.store.status(change.id)
    outputs = status["steps"]["access"]["outputs"]
    assert "endpoints" not in outputs and outputs["activeKeySlot"] == "primary"
    assert APIM_KEY not in json.dumps(status)

    manifest = json.loads(
        (harness.repo / ".citadel" / "changesets" / f"{change.id}.json").read_text(encoding="utf-8")
    )
    assert manifest["executedBy"] == "direct"
    assert manifest["contentHash"] == prepared.content_hash
    assert (harness.repo / "contracts/access/hr/chat/dev/main.bicepparam").is_file()
    assert git(harness.repo, "log", "-1", "--format=%s").startswith("[citadel-agent]")

    with pytest.raises(ChangeError, match="already executed"):
        harness.engine.execute(change.id, prepared.content_hash)
    assert len(harness.runner.deploy_calls) == 1


def test_falls_back_to_pr_without_deploy_rights(seed: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(seed, repo)
    harness = Harness(repo, tmp_path, can_deploy=False)
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    assert prepared.change.mode == "pr"
    assert any("pull request" in w for w in prepared.change.warnings)
    harness.engine.execute(prepared.change.id, prepared.content_hash)
    assert harness.runner.deploy_calls == []
    manifest = json.loads(
        (repo / ".citadel" / "changesets" / f"{prepared.change.id}.json").read_text(encoding="utf-8")
    )
    assert manifest["executedBy"] == "pipeline"


def test_tampered_snapshot_is_rejected(harness: Harness) -> None:
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    snapshot_file = next((harness.store.directory / prepared.change.id / "files").rglob("main.bicepparam"))
    snapshot_file.write_text(snapshot_file.read_text(encoding="utf-8") + "\n// tampered\n", encoding="utf-8")
    with pytest.raises(ChangeError, match="does not match"):
        harness.engine.execute(prepared.change.id, prepared.content_hash)
    assert harness.runner.deploy_calls == []


def test_moved_base_branch_invalidates_approval(harness: Harness) -> None:
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    git(harness.repo, "checkout", "main")
    (harness.repo / "README.md").write_text("moved", encoding="utf-8")
    git(harness.repo, "add", "README.md")
    git(harness.repo, "commit", "-m", "someone else's change")
    with pytest.raises(ChangeError, match="moved since preparation"):
        harness.engine.execute(prepared.change.id, prepared.content_hash)


def test_changed_what_if_invalidates_approval(harness: Harness) -> None:
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    harness.runner.changes.append(("Delete", "/subscriptions/x/resource"))
    with pytest.raises(ChangeError, match="differ from what was approved"):
        harness.engine.execute(prepared.change.id, prepared.content_hash)
    assert harness.runner.deploy_calls == []


def test_known_what_if_noise_is_labelled(harness: Harness) -> None:
    product = f"{APIM_ID}/products/p"
    # The product exists already, so re-creating its API link is known noise.
    harness.runner.changes = [("NoChange", product), ("Create", f"{product}/apis/unified-ai-api")]
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    what_if = prepared.change.steps[0].what_if
    assert what_if is not None
    assert any(line.endswith("[expected: apim-product-api-link]") for line in what_if.changes)
    assert what_if.expected_noise[0].startswith("apim-product-api-link:")
    assert "expected: apim-product-api-link" in prepared.summary()


def test_policy_noise_label_only_when_the_policy_file_is_unchanged(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = f"{APIM_ID}/products/LLM-HR-Chat-DEV/policies/policy"
    noise_paths = ("properties.format (Modify)", "properties.value (Modify)")
    report = WhatIfReport(status="Succeeded", changes=(WhatIfChange(policy, "Modify", noise_paths),))
    monkeypatch.setattr(harness.runner, "what_if", lambda *args: report)
    first = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    assert first.change.steps[0].what_if.expected_noise == []  # a new policy is a real change
    harness.engine.execute(first.change.id, first.content_hash)
    git(harness.repo, "checkout", "main")
    git(harness.repo, "merge", "--ff-only", first.change.branch)
    again = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    assert again.change.steps[0].what_if.expected_noise[0].startswith("apim-rawxml-policy")


def test_children_of_new_resources_are_never_noise(harness: Harness) -> None:
    product = f"{APIM_ID}/products/p"
    harness.runner.changes = [("Create", product), ("Create", f"{product}/apis/unified-ai-api")]
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    what_if = prepared.change.steps[0].what_if
    assert what_if is not None and what_if.expected_noise == []
    assert not any("[expected" in line for line in what_if.changes)


def test_incompatible_live_gateway_blocks_preparation(harness: Harness) -> None:
    harness.live = {**harness.release, "access-contract-version": "2.0.0"}
    with pytest.raises(ChangeError, match="not compatible"):
        harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    harness.live = None
    with pytest.raises(ChangeError, match="not compatible"):
        harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))


def test_key_vault_secret_collisions_are_refused(harness: Harness) -> None:
    first = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    harness.engine.execute(first.change.id, first.content_hash)
    git(harness.repo, "checkout", "main")
    git(harness.repo, "merge", "--ff-only", first.change.branch)
    clash = parse_intent(
        {
            "kind": "access-contract",
            "environment": "dev",
            "metadata": {"owners": ["dev@contoso.com"]},
            "useCase": {"businessUnit": "HR", "useCaseName": "Other", "environment": "DEV"},
            "services": [
                # Normalizes to the first contract's secret name (hr-chat-key).
                {
                    "code": "LLM",
                    "apis": ["universal-llm-api"],
                    "keySecretName": "hr_chat_key",
                    "policy": {"tokenLimit": {"tokensPerMinute": 1000}},
                }
            ],
        }
    )
    with pytest.raises(ChangeError, match="hr-chat-key"):
        harness.engine.prepare(request(IntentChange("other", clash, Operation.ACCESS_WRITE)))
    issues = _validate_with(harness.repo, clash)
    assert any("also written by" in issue for issue in issues)


def _validate_with(repo: Path, intent: Any) -> list[str]:
    from citadel_agent.contracts.intent_io import intent_path, save_intent

    save_intent(intent, repo / Path(*intent_path(intent).parts))
    return _validate(repo)


def test_rotation_state_locks_the_contract_and_travels_with_changes(harness: Harness) -> None:
    from citadel_agent import rotation

    contract_dir = "contracts/access/hr/chat/dev"
    path = rotation.rotation_path(contract_dir)
    state_file = harness.repo / Path(*path.split("/"))
    state = rotation.start(access(), contract_dir, "dev@contoso.com", None)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(rotation.dump_state(state), encoding="utf-8")
    git(harness.repo, "add", "-A")
    git(harness.repo, "commit", "-m", "rotation in progress")

    with pytest.raises(ChangeError, match="rotation is in progress"):
        harness.engine.prepare(request(IntentChange("access", access(tpm=6000), Operation.ACCESS_WRITE)))
    with pytest.raises(ChangeError, match="cannot be part"):
        harness.engine.prepare(
            ChangeRequest(
                summary="Sneak in a policy edit",
                environment="dev",
                changes=(IntentChange("access", access(), Operation.ROTATION_WRITE),),
                extra_files=((".citadel/admin-policy.yaml", "version: 1"),),
            )
        )

    rotate = rotation.advance(state)
    prepared = harness.engine.prepare(
        ChangeRequest(
            summary="Regenerate the old key",
            environment="dev",
            changes=(
                IntentChange("rotation-rotate", rotation.apply(access(), rotate), Operation.ROTATION_WRITE),
            ),
            extra_files=((path, rotation.dump_state(rotate)),),
        )
    )
    assert path in prepared.change.files
    harness.engine.execute(prepared.change.id, prepared.content_hash)
    assert '"phase": "rotate"' in state_file.read_text(encoding="utf-8")
    assert not any("rotation" in issue for issue in _validate(harness.repo))


def test_caps_and_ownership_are_enforced(seed: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(seed, repo)
    consumer = Harness(repo, tmp_path, persona=Persona.CONSUMER)
    with pytest.raises(ChangeError, match="caps"):
        consumer.engine.prepare(request(IntentChange("access", access(tpm=999999), Operation.ACCESS_WRITE)))
    with pytest.raises(PermissionError, match="owner"):
        consumer.engine.prepare(
            request(IntentChange("access", access(owners=["other@contoso.com"]), Operation.ACCESS_WRITE))
        )
    with pytest.raises(PermissionError):
        consumer.engine.prepare(request(IntentChange("access", access(), Operation.BACKEND_WRITE)))


def test_dependent_steps_defer_what_if_and_prerequisites_are_manual(harness: Harness) -> None:
    prepared = harness.engine.prepare(
        request(
            IntentChange("first", access(name="One"), Operation.ACCESS_WRITE),
            IntentChange("second", access(name="Two"), Operation.ACCESS_WRITE, depends_on=("first",)),
            prerequisites=(
                PrerequisiteStep(
                    "rbac", "Grant the gateway identity access", ("az role assignment create ...",), "check"
                ),
            ),
        )
    )
    steps = {s.id: s for s in prepared.change.steps}
    assert steps["first"].what_if is not None and not steps["first"].what_if_deferred
    assert steps["second"].what_if is None and steps["second"].what_if_deferred
    assert [s.id for s in prepared.change.ordered_steps()][:2] == ["first", "second"]
    harness.engine.execute(prepared.change.id, prepared.content_hash)
    assert len(harness.runner.deploy_calls) == 2
    assert harness.store.status(prepared.change.id)["steps"]["rbac"]["status"] == "manual"


# Recording in the canonical repository (the agent works in its own clone) ---------------------


class FakeProvider:
    def __init__(self, pending: list[PullRequestInfo] | None = None) -> None:
        self.opened: list[dict[str, Any]] = []
        self.pending = pending or []

    def open_pull_request(self, **kwargs: Any) -> PullRequestInfo:
        self.opened.append(kwargs)
        return PullRequestInfo(
            number=7, url="https://github.com/contoso/c/pull/7", state="open", merged=False
        )

    def pull_request_status(self, number: int) -> PullRequestInfo:
        raise NotImplementedError

    def open_pull_requests(self, *, labels: list[str]) -> list[PullRequestInfo]:
        return self.pending


@pytest.fixture
def file_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test upstream is a local repository; git blocks local submodule clones by default."""
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'protocol.file.allow'='always'")


def _canonical(seed: Path, tmp_path: Path) -> Path:
    canonical = tmp_path / "canonical"
    shutil.copytree(seed, canonical)
    allow_recording_pushes(canonical)
    return canonical


def _cloned_harness(seed: Path, tmp_path: Path, provider: Any = None) -> tuple[Harness, Path]:
    canonical = _canonical(seed, tmp_path)
    workspace = CanonicalWorkspace(tmp_path / "workspace", remote_url=str(canonical))
    workspace.ensure()
    return Harness(workspace.path, tmp_path, workspace=workspace, git_provider=provider), canonical


@pytest.mark.usefixtures("file_submodules")
def test_direct_change_fast_forwards_a_local_canonical_repository(seed: Path, tmp_path: Path) -> None:
    harness, canonical = _cloned_harness(seed, tmp_path)
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    assert git(canonical, "status", "--porcelain") == ""  # the user's working copy is untouched

    result = harness.engine.execute(prepared.change.id, prepared.content_hash)

    assert result["recorded"] == "base-branch"
    assert git(canonical, "rev-parse", "main") == result["commit"]
    assert (canonical / "contracts/access/hr/chat/dev/main.bicepparam").is_file()
    assert harness.store.status(prepared.change.id)["state"] == "recorded"


@pytest.mark.usefixtures("file_submodules")
def test_refused_recording_after_a_direct_deployment_is_reported(seed: Path, tmp_path: Path) -> None:
    harness, canonical = _cloned_harness(seed, tmp_path)
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    policy_file = canonical / ".citadel" / "admin-policy.yaml"
    policy_file.write_text(policy_file.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")

    result = harness.engine.execute(prepared.change.id, prepared.content_hash)

    assert result["recorded"] == "failed"
    assert "Azure already has this change" in result["next"]
    assert git(canonical, "rev-parse", prepared.change.branch) == result["commit"]
    assert git(canonical, "rev-parse", "main") != result["commit"]
    assert harness.store.status(prepared.change.id)["state"] == "record-failed"


@pytest.mark.usefixtures("file_submodules")
def test_direct_change_on_a_hosted_repository_opens_a_pull_request(
    seed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    harness, canonical = _cloned_harness(seed, tmp_path, provider)
    monkeypatch.setattr(harness.workspace, "remote_is_local", lambda: False)
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))

    result = harness.engine.execute(prepared.change.id, prepared.content_hash)

    assert result["recorded"] == "pull-request" and result["pullRequest"]["number"] == 7
    assert "deployed-directly" in provider.opened[0]["labels"]
    assert "already deployed" in provider.opened[0]["body"]
    assert git(canonical, "rev-parse", prepared.change.branch) == result["commit"]
    assert git(canonical, "rev-parse", "main") != result["commit"]


@pytest.mark.usefixtures("file_submodules")
def test_pending_pull_request_for_the_same_contract_blocks_preparation(
    seed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = PullRequestInfo(
        number=5,
        url="https://github.com/contoso/c/pull/5",
        state="open",
        merged=False,
        files=("contracts/access/hr/chat/dev/contract.yaml",),
    )
    harness, _ = _cloned_harness(seed, tmp_path, FakeProvider([pending]))
    monkeypatch.setattr(harness.workspace, "remote_is_local", lambda: False)
    with pytest.raises(ChangeError, match="pull request #5"):
        harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    other = harness.engine.prepare(
        request(IntentChange("access", access(name="Other"), Operation.ACCESS_WRITE))
    )
    assert other.change.steps[0].contract_dir == "contracts/access/hr/other/dev"


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("C:/repos/contracts", True),
        ("/srv/contracts", True),
        ("file:///srv/contracts", True),
        ("https://github.com/contoso/contracts.git", False),
        ("git@github.com:contoso/contracts.git", False),
        ("ssh://git@host/contoso/contracts", False),
    ],
)
def test_is_local_location(url: str, local: bool) -> None:
    assert is_local_location(url) is local


@pytest.mark.usefixtures("file_submodules")
def test_workspace_refuses_a_clone_of_another_repository(seed: Path, tmp_path: Path) -> None:
    canonical = _canonical(seed, tmp_path)
    CanonicalWorkspace(tmp_path / "workspace", remote_url=str(canonical)).ensure()
    with pytest.raises(WorkspaceError, match="Remove that folder"):
        CanonicalWorkspace(tmp_path / "workspace", remote_url=str(tmp_path / "elsewhere")).ensure()
    with pytest.raises(WorkspaceError, match="CITADEL_AGENT_REPO_URL"):
        CanonicalWorkspace(tmp_path / "missing").ensure()


@pytest.mark.usefixtures("file_submodules")
def test_repo_init_pins_upstream_and_accepts_recording_pushes(seed: Path, tmp_path: Path) -> None:
    upstream = seed.parent / "upstream-src"
    lock = UpstreamLock(
        upstream=UpstreamPin(
            repository="https://github.com/Azure-Samples/ai-hub-gateway-solution-accelerator.git",
            branch="main",
            commit=git(upstream, "rev-parse", "HEAD"),
            path="upstream/ai-hub-gateway",
            release=read_release(upstream),
        ),
        synced_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    target = tmp_path / "new-repo"
    repo = init_canonical_repository(
        target,
        template_dir=Path(__file__).resolve().parents[1] / "templates" / "contracts-repo",
        lock=lock,
        upstream_source=str(upstream),
        admin_policy_text=POLICY,
    )
    assert repo.upstream_commit() == lock.upstream.commit
    assert git(target, "config", "receive.denyCurrentBranch") == "updateInstead"
    assert "github.com/Azure-Samples" in (target / ".gitmodules").read_text(encoding="utf-8")
    assert git(target, "status", "--porcelain") == ""
    with pytest.raises(RepoInitError, match="not empty"):
        init_canonical_repository(
            target,
            template_dir=Path(__file__).resolve().parents[1] / "templates" / "contracts-repo",
            lock=lock,
            upstream_source=str(upstream),
            admin_policy_text=POLICY,
        )


# Repository validation -------------------------------------------------------------------------


def _validate(repo: Path) -> list[str]:
    from citadel_agent.changes.repository import CanonicalRepository
    from citadel_agent.changes.validation import IssueLevel, validate_repository

    issues = validate_repository(CanonicalRepository(repo), load_admin_policy(repo))
    return [f"{i.path}: {i.message}" for i in issues if i.level is IssueLevel.ERROR]


def test_repository_produced_by_the_engine_validates_and_tampering_is_caught(harness: Harness) -> None:
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    harness.engine.execute(prepared.change.id, prepared.content_hash)
    assert _validate(harness.repo) == []

    contract_dir = harness.repo / "contracts" / "access" / "hr" / "chat" / "dev"
    policy_file = contract_dir / "llm-product-policy.xml"
    policy_file.write_text(
        policy_file.read_text(encoding="utf-8").replace("gpt-5.2", "gpt-4o"), encoding="utf-8"
    )
    (contract_dir / "evil.bicep").write_text("targetScope = 'subscription'", encoding="utf-8")
    (contract_dir / "notes.md").write_text(f"key {APIM_KEY}", encoding="utf-8")
    contract = contract_dir / "contract.yaml"
    contract.write_text(
        contract.read_text(encoding="utf-8").replace("keyRotationEnabled: false", "keyRotationEnabled: true"),
        encoding="utf-8",
    )
    errors = "\n".join(_validate(harness.repo))
    assert "out of date" in errors
    assert "templates are not allowed" in errors
    assert "secret-shaped" in errors
    assert "active rotation transaction" in errors


def test_pr_manifest_detects_edited_files(seed: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(seed, repo)
    harness = Harness(repo, tmp_path, can_deploy=False)
    prepared = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    harness.engine.execute(prepared.change.id, prepared.content_hash)
    assert _validate(repo) == []
    contract = repo / "contracts" / "access" / "hr" / "chat" / "dev" / "contract.yaml"
    contract.write_text(contract.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert any("differs from the prepared change set" in e for e in _validate(repo))


def test_newer_change_set_supersedes_older_manifest_files(seed: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(seed, repo)
    harness = Harness(repo, tmp_path, can_deploy=False)
    first = harness.engine.prepare(request(IntentChange("access", access(), Operation.ACCESS_WRITE)))
    harness.engine.execute(first.change.id, first.content_hash)
    contract = repo / "contracts" / "access" / "hr" / "chat" / "dev" / "contract.yaml"
    manifests = repo / ".citadel" / "changesets"

    # A later change set rewrites the same contract file and records its own digest.
    contract.write_text(contract.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    digest = hashlib.sha256(contract.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    later = json.loads((manifests / f"{first.change.id}.json").read_text(encoding="utf-8"))
    later["files"] = {"contracts/access/hr/chat/dev/contract.yaml": f"sha256:{digest}"}
    (manifests / "cs-99991231-235959-zzzzzz.json").write_text(json.dumps(later), encoding="utf-8")

    errors = [e for e in _validate(repo) if "differs from the prepared change set" in e]
    assert errors == []
