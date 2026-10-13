# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Composition root: wires settings, credentials, policy, workspace and services together."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError

from citadel_agent import __version__
from citadel_agent.azure_clients.apim import ApimClient
from citadel_agent.azure_clients.arm import ArmClient, ArmError, PermissionSet
from citadel_agent.azure_clients.credentials import PrincipalInfo, create_credential, describe_principal
from citadel_agent.azure_clients.deployments import DeploymentRunner
from citadel_agent.azure_clients.keyvault import KeyVaultReader, vault_url
from citadel_agent.changes.engine import ChangeEngine, EngineServices
from citadel_agent.changes.git_providers import GitHubProvider, GitProviderError, github_token
from citadel_agent.changes.store import ChangeStore
from citadel_agent.changes.workspace import CanonicalWorkspace, WorkspaceError
from citadel_agent.gateway.client import GatewayClient
from citadel_agent.knowledge.docs import DocIndex, cached_doc_index
from citadel_agent.paths import find_project_root, state_dir
from citadel_agent.policy.admin import (
    AdminPolicy,
    EnvironmentPolicy,
    LoadedPolicy,
    load_admin_policy,
    load_bootstrap_policy,
)
from citadel_agent.policy.authz import Authorizer, Caller, Persona, derive_persona
from citadel_agent.proc import CommandError
from citadel_agent.safety.audit import AuditLog
from citadel_agent.settings import Settings
from citadel_agent.upstream.lock import UpstreamLock, read_lock

logger = logging.getLogger(__name__)
DEPLOY_ACTION = "Microsoft.Resources/deployments/write"


def apim_resource_id(env: EnvironmentPolicy) -> str:
    a = env.apim
    return (
        f"/subscriptions/{a.subscription_id}/resourceGroups/{a.resource_group_name}"
        f"/providers/Microsoft.ApiManagement/service/{a.name}"
    )


@dataclass
class AgentRuntime:
    project_root: Path
    settings: Settings
    credential: TokenCredential
    lock: UpstreamLock
    workspace: CanonicalWorkspace
    loaded: LoadedPolicy
    principal: PrincipalInfo
    caller: Caller
    store: ChangeStore
    audit: AuditLog
    arm: ArmClient
    _gateways: dict[str, GatewayClient] = field(default_factory=dict)

    @property
    def policy(self) -> AdminPolicy | None:
        return self.loaded.policy

    @property
    def authorizer(self) -> Authorizer:
        return Authorizer(self.loaded, self.caller)

    def environment_name(self, name: str | None = None) -> str:
        if name:
            return name
        if self.settings.environment:
            return self.settings.environment
        if self.policy is None:
            raise RuntimeError(self.loaded.error or "No admin policy is loaded")
        return self.policy.default_environment

    def environment(self, name: str | None = None) -> EnvironmentPolicy:
        if self.policy is None:
            raise RuntimeError(self.loaded.error or "No admin policy is loaded")
        return self.policy.environment(self.environment_name(name))

    def gateway(self, name: str | None = None) -> GatewayClient:
        key = self.environment_name(name)
        if key not in self._gateways:
            self._gateways[key] = GatewayClient(self.environment(key).gateway_url)
        return self._gateways[key]

    def apim(self, name: str | None = None) -> ApimClient:
        env = self.environment(name)
        return ApimClient(
            self.credential, env.apim.subscription_id, env.apim.resource_group_name, env.apim.name
        )

    def key_vault(self, name: str | None = None) -> KeyVaultReader | None:
        env = self.environment(name)
        return KeyVaultReader(self.credential, vault_url(env.key_vault.name)) if env.key_vault else None

    def live_release(self, env: EnvironmentPolicy) -> dict[str, str] | None:
        try:
            return GatewayClient(env.gateway_url).version()
        except Exception as exc:  # unknown live state blocks writes (fail closed)
            logger.warning("Could not read %s/version: %s", env.gateway_url, exc)
            return None

    def permissions(self, scope: str) -> PermissionSet | None:
        try:
            return self.arm.permissions(scope)
        except (ArmError, ClientAuthenticationError) as exc:
            logger.warning("Could not read permissions at %s: %s", scope, exc)
            return None

    def can_deploy(self, env: EnvironmentPolicy) -> bool:
        permissions = self.permissions(f"/subscriptions/{env.subscription_id}")
        return bool(permissions and permissions.allows(DEPLOY_ACTION))

    def docs(self) -> DocIndex:
        """Search index over the agent's pinned upstream documentation (built once per process)."""
        root = self.project_root / Path(*self.lock.upstream.path.split("/"))
        return cached_doc_index(str(root), self.lock.upstream.repository, self.lock.upstream.commit)

    def git_provider(self) -> GitHubProvider | None:
        if self.policy is None or self.policy.gitops.provider != "github" or not github_token():
            return None
        try:
            return GitHubProvider(self.policy.gitops.repository)
        except GitProviderError as exc:
            logger.warning("GitHub provider unavailable: %s", exc)
            return None

    def engine(self) -> ChangeEngine:
        settings = self.settings

        def reload() -> LoadedPolicy:
            if settings.bootstrap_policy:
                return load_bootstrap_policy(settings.bootstrap_policy)
            return load_admin_policy(self.workspace.path, base_branch=settings.base_branch)

        return ChangeEngine(
            EngineServices(
                workspace=self.workspace,
                store=self.store,
                authorizer=self.authorizer,
                agent_lock=self.lock,
                agent_version=__version__,
                runner_factory=lambda subscription: DeploymentRunner(self.credential, subscription),
                live_release=self.live_release,
                can_deploy=self.can_deploy,
                audit=self.audit,
                git_provider=self.git_provider(),
                reload_policy=reload,
            )
        )

    def describe(self) -> dict[str, Any]:
        return {
            "agentVersion": __version__,
            "caller": self.principal.label,
            "persona": self.caller.persona.value,
            "policySource": self.loaded.source.value if self.loaded.source else None,
            "policyError": self.loaded.error,
            "repository": self.settings.repo_url,
            "workspace": str(self.workspace.path),
            "upstreamCommit": self.lock.upstream.commit,
        }


def _load_policy(settings: Settings, workspace: CanonicalWorkspace) -> LoadedPolicy:
    if settings.bootstrap_policy:
        return load_bootstrap_policy(settings.bootstrap_policy)
    try:
        workspace.ensure()
    except (WorkspaceError, CommandError) as exc:
        return LoadedPolicy(policy=None, source=None, error=str(exc))
    return load_admin_policy(workspace.path, base_branch=settings.base_branch)


def build_runtime(
    settings: Settings | None = None, *, credential: TokenCredential | None = None
) -> AgentRuntime:
    """Create the runtime from settings. Policy problems leave the agent read-only (fail closed)."""
    settings = settings or Settings()
    project_root = find_project_root()
    lock = read_lock(project_root)
    state = settings.state_dir or state_dir(project_root)
    workspace = CanonicalWorkspace(
        settings.workspace_dir or state / "repo",
        remote_url=settings.repo_url,
        base_branch=settings.base_branch,
    )
    credential = credential or create_credential()
    arm = ArmClient(credential)
    principal = describe_principal(credential)
    loaded = _load_policy(settings, workspace)

    persona = Persona.CONSUMER
    if loaded.policy is not None:
        env = loaded.policy.environment(settings.environment)
        try:
            permissions = arm.permissions(apim_resource_id(env))
        except (ArmError, ClientAuthenticationError) as exc:
            # Unknown permissions mean the least privileged persona (fail closed).
            logger.warning("Could not read APIM permissions: %s", exc)
            permissions = None
        persona = derive_persona(loaded.policy, permissions)
    audit = AuditLog(state / "audit")
    return AgentRuntime(
        project_root=project_root,
        settings=settings,
        credential=credential,
        lock=lock,
        workspace=workspace,
        loaded=loaded,
        principal=principal,
        caller=Caller(principal, persona),
        store=ChangeStore(state / "changes"),
        audit=audit,
        arm=arm,
    )
