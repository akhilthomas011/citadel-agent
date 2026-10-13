# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Admin policy: environments, change modes, caps and allowed operations.

The policy lives in the canonical contracts repository at ``.citadel/admin-policy.yaml`` and is
owned by platform admins (CODEOWNERS plus branch protection). The agent reads the version
committed on the protected base branch, never the working copy, and records the blob SHA in
every change set. Anything missing or invalid makes the agent fail closed (read-only).

Hard rules enforced at load time:

* prod-class environments always use pull requests (``mode: direct`` is rejected)
* direct-output credentials (keys in deployment outputs) are off unless explicitly allowed
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from citadel_agent.contracts.common import AzureResourceRef, CitadelModel, GuidStr, HttpsUrl
from citadel_agent.contracts.targets import EnvironmentClass, EnvironmentTarget, ManagedIdentityRef
from citadel_agent.proc import CommandError, run_command

ADMIN_POLICY_REPO_PATH = ".citadel/admin-policy.yaml"


class ChangeMode(StrEnum):
    DIRECT = "direct"
    PR = "pr"


class Operation(StrEnum):
    """Operations the agent can perform. Read operations never change Azure or the repository."""

    GATEWAY_READ = "gateway.read"
    MODELS_READ = "models.read"
    CODEGEN = "codegen"
    TROUBLESHOOT = "troubleshoot"
    SMOKE_TEST = "smoke-test"
    USAGE_READ_OWN = "usage.read.own"
    USAGE_READ_ALL = "usage.read.all"
    CONTRACTS_READ_OWN = "contracts.read.own"
    CONTRACTS_READ_ALL = "contracts.read.all"
    DOCS = "docs"
    ACCESS_WRITE = "access.write"
    ROTATION_WRITE = "rotation.write"
    ALERTS_WRITE = "alerts.write"
    PUBLISH_WRITE = "publish.write"
    BACKEND_WRITE = "backend.write"


WRITE_OPERATIONS = frozenset(
    {
        Operation.ACCESS_WRITE,
        Operation.ROTATION_WRITE,
        Operation.ALERTS_WRITE,
        Operation.PUBLISH_WRITE,
        Operation.BACKEND_WRITE,
    }
)
DEFAULT_CONSUMER_OPERATIONS: tuple[str, ...] = (
    Operation.GATEWAY_READ,
    Operation.MODELS_READ,
    Operation.CODEGEN,
    Operation.TROUBLESHOOT,
    Operation.SMOKE_TEST,
    Operation.USAGE_READ_OWN,
    Operation.CONTRACTS_READ_OWN,
    Operation.DOCS,
    Operation.ACCESS_WRITE,
    Operation.ROTATION_WRITE,
    Operation.ALERTS_WRITE,
)


class Caps(CitadelModel):
    """Limits the agent enforces on contract intents before anything is deployed or proposed."""

    max_tokens_per_minute: int | None = Field(default=None, ge=1)
    max_monthly_token_quota: int | None = Field(default=None, ge=1)
    allowed_models: list[str] = Field(default_factory=list)
    allowed_service_codes: list[str] = Field(default_factory=lambda: ["LLM", "TOOL", "AGENT", "MULTI"])
    allow_direct_output_credentials: bool = False
    allow_response_headers: bool = True


class CosmosSettings(CitadelModel):
    account_name: str
    database: str | None = None


class EnvironmentPolicy(CitadelModel):
    """One Citadel environment the agent can work with."""

    environment_class: EnvironmentClass = Field(alias="class")
    mode: ChangeMode = ChangeMode.PR
    fallback_to_pr: bool = Field(default=True, description="Open a PR when the caller cannot deploy directly")
    direct_recording: Literal["auto-pr", "env-branch"] = "auto-pr"
    subscription_id: GuidStr
    location: str
    tenant_id: GuidStr | None = None
    apim: AzureResourceRef
    gateway_url: HttpsUrl
    apim_managed_identity: ManagedIdentityRef | None = None
    key_vault: AzureResourceRef | None = None
    app_insights: AzureResourceRef | None = None
    log_analytics_workspace_id: GuidStr | None = None
    cosmos: CosmosSettings | None = None
    caps: Caps = Field(default_factory=Caps)
    allow_prerequisite_actions: bool = False

    @model_validator(mode="after")
    def _hard_rules(self) -> EnvironmentPolicy:
        if self.environment_class == "prod":
            if self.mode is ChangeMode.DIRECT:
                raise ValueError("prod-class environments must use mode: pr")
            if self.allow_prerequisite_actions:
                raise ValueError("prod-class environments cannot allow prerequisite actions")
            if self.caps.allow_direct_output_credentials:
                raise ValueError("prod-class environments cannot allow direct-output credentials")
        return self

    def to_target(self, name: str) -> EnvironmentTarget:
        return EnvironmentTarget(
            name=name,
            environment_class=self.environment_class,
            subscription_id=self.subscription_id,
            location=self.location,
            tenant_id=self.tenant_id,
            apim=self.apim,
            gateway_url=self.gateway_url,
            apim_managed_identity=self.apim_managed_identity,
            key_vault=self.key_vault,
            app_insights=self.app_insights,
        )


class OperationsPolicy(CitadelModel):
    consumer: list[str] = Field(default_factory=lambda: list(DEFAULT_CONSUMER_OPERATIONS))
    operator: list[str] = Field(default_factory=lambda: ["*"])


class PersonaPolicy(CitadelModel):
    """How the operator persona is granted (it is derived, never self-selected)."""

    operator_requires_apim_write: bool = True
    operator_group_object_ids: list[GuidStr] = Field(default_factory=list)


class GitOpsPolicy(CitadelModel):
    provider: Literal["github", "azure-devops"] = "github"
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = "main"
    upstream_path: str = "upstream/ai-hub-gateway"
    reviewers: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=lambda: ["citadel-agent"])


class AgentSettings(CitadelModel):
    """Settings for the agent's own LLM access through the gateway."""

    environment: str
    model: str = "gpt-5.2"
    gateway_key_secret_name: str = "CITADEL-AGENT-LLM-KEY"  # noqa: S105 - a Key Vault secret name, not a secret


class AdminPolicy(CitadelModel):
    """The complete admin policy document."""

    version: Literal[1] = 1
    default_environment: str
    environments: dict[str, EnvironmentPolicy] = Field(min_length=1)
    operations: OperationsPolicy = Field(default_factory=OperationsPolicy)
    personas: PersonaPolicy = Field(default_factory=PersonaPolicy)
    gitops: GitOpsPolicy
    agent: AgentSettings | None = None

    @model_validator(mode="after")
    def _references(self) -> AdminPolicy:
        if self.default_environment not in self.environments:
            raise ValueError(f"defaultEnvironment '{self.default_environment}' is not defined")
        if self.agent and self.agent.environment not in self.environments:
            raise ValueError(f"agent.environment '{self.agent.environment}' is not defined")
        known = {op.value for op in Operation} | {"*"}
        for persona, operations in (
            ("consumer", self.operations.consumer),
            ("operator", self.operations.operator),
        ):
            unknown = sorted(set(operations) - known)
            if unknown:
                raise ValueError(f"Unknown {persona} operations: {', '.join(unknown)}")
        return self

    def environment(self, name: str | None = None) -> EnvironmentPolicy:
        key = name or self.default_environment
        if key not in self.environments:
            raise KeyError(f"Environment '{key}' is not defined in the admin policy")
        return self.environments[key]


class PolicySource(StrEnum):
    REPOSITORY = "repository"
    LOCAL_BOOTSTRAP = "local-bootstrap"


@dataclass(frozen=True)
class LoadedPolicy:
    """An admin policy plus where it came from (recorded in change sets)."""

    policy: AdminPolicy | None
    source: PolicySource | None
    ref: str | None = None
    blob_sha: str | None = None
    error: str | None = None

    @property
    def writable(self) -> bool:
        """Writes are only possible with a valid policy."""
        return self.policy is not None


class AdminPolicyError(ValueError):
    """Raised when the admin policy cannot be parsed or violates hard rules."""


def parse_admin_policy(text: str) -> AdminPolicy:
    try:
        return AdminPolicy.model_validate(yaml.safe_load(text))
    except (yaml.YAMLError, ValidationError) as exc:
        raise AdminPolicyError(f"Invalid admin policy: {exc}") from exc


def _resolve_ref(repo_root: Path, base_branch: str) -> str:
    for candidate in (f"origin/{base_branch}", base_branch):
        result = run_command(
            "git", ["rev-parse", "--verify", "--quiet", candidate], cwd=repo_root, check=False
        )
        if result.returncode == 0:
            return candidate
    raise AdminPolicyError(f"Neither origin/{base_branch} nor {base_branch} exists in {repo_root}")


def load_admin_policy(repo_root: Path, *, base_branch: str = "main", ref: str | None = None) -> LoadedPolicy:
    """Load the committed admin policy from the protected base branch (or an explicit ``ref``). Fails closed.

    Local edits to the working copy are ignored on purpose: only reviewed, committed policy
    applies.
    """
    try:
        ref = ref or _resolve_ref(repo_root, base_branch)
        spec = f"{ref}:{ADMIN_POLICY_REPO_PATH}"
        text = run_command("git", ["show", spec], cwd=repo_root).stdout
        blob = run_command("git", ["rev-parse", spec], cwd=repo_root).stdout.strip()
        policy = parse_admin_policy(text)
    except (AdminPolicyError, CommandError) as exc:
        return LoadedPolicy(policy=None, source=None, error=str(exc))
    return LoadedPolicy(policy=policy, source=PolicySource.REPOSITORY, ref=ref, blob_sha=blob)


def load_bootstrap_policy(path: Path) -> LoadedPolicy:
    """Load a local policy file for dev bootstrap only. Non-dev environments are rejected."""
    try:
        policy = parse_admin_policy(path.read_text(encoding="utf-8"))
        non_dev = [name for name, env in policy.environments.items() if env.environment_class != "dev"]
        if non_dev:
            raise AdminPolicyError(
                "A local bootstrap policy may only define dev-class environments "
                f"(found: {', '.join(non_dev)})"
            )
    except (AdminPolicyError, OSError) as exc:
        return LoadedPolicy(policy=None, source=None, error=str(exc))
    return LoadedPolicy(policy=policy, source=PolicySource.LOCAL_BOOTSTRAP, ref=str(path))
