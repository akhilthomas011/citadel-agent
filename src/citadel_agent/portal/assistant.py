# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Scoped, durable hosted conversations and immutable, explicitly approved changes."""

from __future__ import annotations

import base64
import copy
import hashlib
import logging
import re
import secrets
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from fastapi import BackgroundTasks
from openai import APIError

from citadel_agent.agent import build_registry, create_agent
from citadel_agent.azure_clients.arm import ArmClient
from citadel_agent.azure_clients.credentials import PrincipalInfo
from citadel_agent.changes.approval import approval_details, git_base_reader
from citadel_agent.changes.changeset import ChangeSet
from citadel_agent.changes.drafts import DraftStore
from citadel_agent.changes.engine import ChangeError, PreparedChange
from citadel_agent.changes.git_providers import GitHubProvider, GitProviderError
from citadel_agent.changes.store import ChangeStore, ChangeStoreError
from citadel_agent.changes.workspace import CanonicalWorkspace, WorkspaceError
from citadel_agent.contracts.publish import PublishContractIntent
from citadel_agent.paths import find_project_root
from citadel_agent.policy.admin import (
    WRITE_OPERATIONS,
    EnvironmentPolicy,
    LoadedPolicy,
    Operation,
    load_admin_policy,
)
from citadel_agent.policy.authz import AuthorizationError, Authorizer, Caller, Persona
from citadel_agent.portal.agent_auth import identity_from_session
from citadel_agent.portal.approvals import ApprovalEvidence, ApprovalStatus, PortalApprovalService
from citadel_agent.portal.auth import PortalIdentity
from citadel_agent.portal.authz import (
    HubGrant,
    PortalAction,
    PortalAuthorizationError,
    PortalGrantPermission,
    authorize_portal_action,
)
from citadel_agent.portal.contracts import PortalRole
from citadel_agent.portal.product_contracts import (
    ChangeDecision,
    ChangeResponse,
    ChatMessage,
    ConversationResponse,
    PortalHubConfig,
    TurnRequest,
)
from citadel_agent.portal.sessions import PortalSessionStore, PortalStateConflict
from citadel_agent.portal.state import ProductStore, VersionedDocument
from citadel_agent.proc import CommandError
from citadel_agent.runtime import AgentRuntime
from citadel_agent.safety.audit import AuditLog, AuditOutcome
from citadel_agent.safety.redaction import default_redactor
from citadel_agent.safety.secret_ingress import find_secret_literals
from citadel_agent.settings import Settings
from citadel_agent.tools.context import ToolContext
from citadel_agent.upstream.lock import read_lock

logger = logging.getLogger(__name__)
_TURN_LEASE_SECONDS = 600

if TYPE_CHECKING:
    from citadel_agent.portal.api import HubGrantReader
    from citadel_agent.portal.data import PortalDataService


class AssistantUnavailable(RuntimeError):
    """The model or one of the assistant's configured dependencies is unavailable."""


class HostedAuthorizer(Authorizer):
    """Portal roles and explicit sensitive-data grants narrow, never widen, the admin policy."""

    def __init__(self, loaded: LoadedPolicy, caller: Caller, identity: PortalIdentity, grant: HubGrant):
        super().__init__(loaded, caller)
        self.identity = identity
        self.grant = grant

    def require(self, operation: Operation) -> None:
        if operation in WRITE_OPERATIONS and PortalRole.OPERATOR not in self.identity.roles:
            raise AuthorizationError("Only a platform operator can draft changes.")
        if operation in {Operation.USAGE_READ_ALL, Operation.USAGE_READ_OWN} and not (
            PortalRole.FINOPS_ANALYST in self.identity.roles
            and PortalGrantPermission.FINOPS_DATA in self.grant.permissions
        ):
            raise AuthorizationError("FinOps role and data grant are required for usage tools.")
        if (
            operation is Operation.TROUBLESHOOT
            and PortalGrantPermission.RAW_LOGS not in self.grant.permissions
        ):
            raise AuthorizationError("Raw-log tools require an explicit raw-logs grant.")
        if operation is Operation.SMOKE_TEST:
            raise AuthorizationError("Billable smoke tests are not exposed through the hosted assistant.")
        super().require(operation)


class HostedRuntime(AgentRuntime):
    portal_authorizer: HostedAuthorizer
    github_token_value: str | None = None

    @property
    def authorizer(self) -> HostedAuthorizer:
        return self.portal_authorizer

    def git_provider(self) -> GitHubProvider | None:
        if not self.github_token_value or self.policy is None:
            return None
        return GitHubProvider(self.policy.gitops.repository, token=self.github_token_value)

    def can_deploy(self, env: EnvironmentPolicy) -> bool:
        return False


class CosmosAuditLog(AuditLog):
    def __init__(self, store: ProductStore, subject_id: str):
        super().__init__(Path("."), redactor=default_redactor())
        self.store = store
        self.subject_id = subject_id

    def record(
        self,
        event: str,
        *,
        outcome: AuditOutcome = AuditOutcome.OK,
        tool: str | None = None,
        actor: str | None = None,
        details: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        entry = {
            "event": event,
            "outcome": outcome.value,
            "tool": tool,
            "actor": self.subject_id,
            "timestamp": (now or datetime.now(UTC)).isoformat(),
            "details": default_redactor().redact_obj(dict(details or {})),
        }
        self.store.create("portal-audit", secrets.token_hex(32), self.subject_id, entry)
        return entry


class CosmosChangeStore(ChangeStore):
    """The existing change engine uses Cosmos snapshots/status instead of ephemeral local files."""

    def __init__(
        self, directory: Path, store: ProductStore, subject_id: str, approvals: PortalApprovalService
    ) -> None:
        super().__init__(directory)
        self.product_store = store
        self.subject_id = subject_id
        self.approvals = approvals
        self.workspace: Path | None = None

    def save(self, change: ChangeSet, files: dict[str, str]) -> Path:
        if change.environment != self.product_store.scope.environment or change.mode != "pr":
            raise ChangeError("Hosted changes require the configured hub environment and PR-only mode.")
        if find_secret_literals(files):
            raise ChangeError("The prepared snapshot contains a secret-shaped value.")
        if self.workspace is None:
            raise RuntimeError("The hosted change store has no canonical workspace.")
        preview = (
            PreparedChange(change, change.content_hash()).summary()
            + "\n\n"
            + approval_details(change.files, files, git_base_reader(self.workspace, change.repo_base_commit))
        )
        evidence = _evidence(change, preview)
        approval = self.approvals.create(
            change_id=change.id,
            scope=self.product_store.scope,
            author_id=self.subject_id,
            evidence=evidence,
            now=datetime.now(UTC),
            ttl_seconds=3600,
        )
        self.product_store.create(
            "hosted-change",
            change.id,
            self.subject_id,
            {
                "change": change.model_dump(by_alias=True, mode="json"),
                "files": files,
                "preview": preview,
                "approvalId": approval.record.id,
                "status": {"steps": {}, "events": []},
                "executionStatus": "prepared",
            },
        )
        return self.directory / change.id

    def load(self, change_id: str) -> ChangeSet:
        return ChangeSet.model_validate(self._get(change_id).record.data["change"])

    def snapshot(self, change_id: str) -> dict[str, str]:
        return dict(self._get(change_id).record.data["files"])

    def status(self, change_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._get(change_id).record.data["status"])

    def record(self, change_id: str, event: str, **details: Any) -> dict[str, Any]:
        current = self._get(change_id)
        data = copy.deepcopy(current.record.data)
        status = data["status"]
        status["events"].append({"ts": datetime.now(UTC).isoformat(), "event": event, **details})
        step = details.get("step")
        if step:
            status["steps"].setdefault(step, {}).update({k: v for k, v in details.items() if k != "step"})
        for key in ("pullRequest", "recordedCommit", "state", "next", "recordError"):
            if key in details:
                status[key] = details[key]
        self.product_store.replace(current, data)
        return status

    def list_ids(self) -> list[str]:
        return [item.record.key for item in self.product_store.list("hosted-change")]

    def _get(self, change_id: str) -> VersionedDocument:
        try:
            return self.product_store.get("hosted-change", change_id)
        except KeyError as exc:
            raise ChangeStoreError("Prepared change was not found in this hub.") from exc


class HostedRuntimeFactory:
    def __init__(
        self,
        config: PortalHubConfig,
        credential: TokenCredential,
        store: ProductStore,
        *,
        github_token: str | None = None,
    ):
        self.config = config
        self.credential = credential
        self.store = store
        self.approvals = PortalApprovalService(store.container)
        self.github_token = github_token
        default_redactor().register_secret(github_token)
        self.git_environment = {"GIT_TERMINAL_PROMPT": "0"}
        if github_token:
            encoded = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
            default_redactor().register_secret(encoded)
            self.git_environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
                }
            )

    @contextmanager
    def workspace(self):
        with TemporaryDirectory(prefix="citadel-portal-") as temporary:
            root = Path(temporary)
            workspace = CanonicalWorkspace(
                root / "repo",
                remote_url=self.config.contracts_repository_url,
                base_branch=self.config.contracts_branch,
                git_environment=self.git_environment,
            )
            workspace.ensure()
            yield root, workspace

    def create(
        self, root: Path, workspace: CanonicalWorkspace, identity: PortalIdentity, grant: HubGrant
    ) -> HostedRuntime:
        loaded = load_admin_policy(workspace.path, base_branch=self.config.contracts_branch)
        if loaded.policy is None:
            raise AssistantUnavailable("The canonical admin policy is unavailable.")
        environment = loaded.policy.environment(self.config.scope.environment)
        if (
            environment.subscription_id != self.config.scope.subscription_id
            or environment.apim.subscription_id != self.config.scope.subscription_id
            or environment.apim.name != self.config.apim_name
            or environment.apim.resource_group_name.lower() != self.config.apim_resource_group.lower()
            or environment.gateway_url.rstrip("/") != self.config.gateway_url.rstrip("/")
        ):
            raise AssistantUnavailable("The canonical environment does not match the configured hub.")
        policy = loaded.policy.model_copy(
            update={
                "environments": {self.config.scope.environment: environment},
                "default_environment": self.config.scope.environment,
            }
        )
        scoped_loaded = LoadedPolicy(
            policy=policy, source=loaded.source, ref=loaded.ref, blob_sha=loaded.blob_sha, error=loaded.error
        )
        principal = PrincipalInfo(identity.subject_id, identity.tenant_id, identity.subject_id, "User")
        persona = Persona.OPERATOR if PortalRole.OPERATOR in identity.roles else Persona.CONSUMER
        caller = Caller(principal, persona)
        change_store = CosmosChangeStore(
            root / "state" / "changes", self.store, identity.subject_id, self.approvals
        )
        change_store.workspace = workspace.path
        runtime = HostedRuntime(
            project_root=find_project_root(),
            settings=Settings(
                _env_file=None,
                repo_url=self.config.contracts_repository_url,
                workspace_dir=workspace.path,
                base_branch=self.config.contracts_branch,
                bootstrap_policy=None,
                environment=self.config.scope.environment,
                state_dir=root / "state",
                gateway_key=None,
                model=self.config.model_deployment,
            ),
            credential=self.credential,
            lock=read_lock(find_project_root()),
            workspace=workspace,
            loaded=scoped_loaded,
            principal=principal,
            caller=caller,
            store=change_store,
            audit=CosmosAuditLog(self.store, identity.subject_id),
            arm=ArmClient(self.credential),
        )
        runtime.portal_authorizer = HostedAuthorizer(scoped_loaded, caller, identity, grant)
        runtime.github_token_value = self.github_token
        return runtime

    def canonical_counts(self) -> dict[str, int]:
        try:
            with self.workspace() as (_root, workspace):
                records = [
                    record.intent
                    for record in workspace.repository.intents()
                    if record.intent.environment == self.config.scope.environment
                ]
            assets = [
                asset
                for intent in records
                if isinstance(intent, PublishContractIntent)
                for asset in intent.assets
            ]
            return {
                "access_contracts": sum(intent.kind == "access-contract" for intent in records),
                "mcp_tools": sum(asset.asset_type in {"mcp-from-api", "mcp-existing"} for asset in assets),
                "published_agents": sum(asset.asset_type == "a2a" for asset in assets),
            }
        except (WorkspaceError, CommandError, ValueError, OSError):
            logger.warning("Canonical portal inventory could not be read.")
            return {}


class PortalAssistantService:
    def __init__(
        self,
        store: ProductStore,
        factory: HostedRuntimeFactory,
        *,
        runner: Callable | None = None,
        data_service: PortalDataService | None = None,
        durable_turns: bool = False,
        worker_sessions: PortalSessionStore | None = None,
        worker_grants: HubGrantReader | None = None,
    ):
        if data_service is not None and data_service.config.scope != store.scope:
            raise ValueError("assistant and dashboard services must use the same hub scope")
        self.store = store
        self.factory = factory
        self.runner = runner or self._run
        self.data_service = data_service
        self.durable_turns = durable_turns
        self.worker_sessions = worker_sessions
        self.worker_grants = worker_grants

    def create_conversation(self, identity: PortalIdentity) -> ConversationResponse:
        key = secrets.token_hex(32)
        document = self.store.create(
            "conversation",
            key,
            identity.subject_id,
            {"title": "New conversation", "messages": [], "status": "idle", "state": {}, "drafts": {}},
        )
        return self._response(document)

    def conversations(self, identity: PortalIdentity) -> list[ConversationResponse]:
        return [
            self._response(item)
            for item in self.store.list("conversation", subject_id=identity.subject_id, limit=30)
        ]

    def conversation(self, conversation_id: str, identity: PortalIdentity) -> ConversationResponse:
        current = self.store.get("conversation", conversation_id, subject_id=identity.subject_id)
        data = copy.deepcopy(current.record.data)
        if data["status"] == "running" and datetime.fromisoformat(data["leaseUntil"]) <= datetime.now(UTC):
            data.update(
                status="queued"
                if self.durable_turns and not data.get("executionStarted", True)
                else "failed",
                errorCode=None
                if self.durable_turns and not data.get("executionStarted", True)
                else "turn-interrupted",
            )
            current = self.store.replace(current, data)
        return self._response(current)

    async def turn(
        self,
        conversation_id: str,
        request: TurnRequest,
        identity: PortalIdentity,
        grant: HubGrant,
        *,
        background_tasks: BackgroundTasks | None = None,
        session_reference: str | None = None,
    ) -> ConversationResponse:
        from starlette.concurrency import run_in_threadpool

        if find_secret_literals(request.message):
            raise ValueError(
                "Do not send secrets in chat. Rotate pasted credentials and use Key Vault references."
            )
        if self.durable_turns and not self.worker_available():
            raise AssistantUnavailable(
                "The durable assistant worker is unavailable. Retry after it recovers."
            )
        current = await run_in_threadpool(
            self.store.get, "conversation", conversation_id, subject_id=identity.subject_id
        )
        data = copy.deepcopy(current.record.data)
        request_id = str(request.request_id)
        digest = hashlib.sha256(request.message.encode()).hexdigest()
        if data.get("requestId") == request_id:
            if data.get("requestHash") != digest:
                raise PortalStateConflict("This request ID was already used for a different message.")
            return self._response(current)
        if data["status"] in {"queued", "running"}:
            if data["status"] == "running" and datetime.fromisoformat(data["leaseUntil"]) <= datetime.now(
                UTC
            ):
                data.update(status="failed", errorCode="turn-interrupted")
                await run_in_threadpool(self.store.replace, current, data)
            raise PortalStateConflict(
                "The previous turn is running or was interrupted; reload before retrying."
            )
        if len(data["messages"]) >= 80:
            raise PortalStateConflict("This conversation reached 40 turns; start a new conversation.")
        data.update(
            status="queued" if self.durable_turns else "running",
            errorCode=None,
            requestId=request_id,
            requestHash=digest,
            leaseUntil=(datetime.now(UTC) + timedelta(seconds=_TURN_LEASE_SECONDS)).isoformat(),
            title=data["title"] if data["messages"] else request.message[:80],
        )
        if self.durable_turns:
            if session_reference is None or re.fullmatch(r"[0-9a-f]{64}", session_reference) is None:
                raise ValueError("Durable turns require a verified server-side session reference.")
            data["queuedTurn"] = {
                "sessionReference": session_reference,
                "requestId": request_id,
                "requestHash": digest,
                "message": request.message,
            }
        data["messages"].append(
            {
                "role": "user",
                "content": request.message,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        claimed = await run_in_threadpool(self.store.replace, current, data)
        if self.durable_turns:
            return self._response(claimed)
        if background_tasks is not None:
            background_tasks.add_task(self._finish_background, claimed, data, request, identity, grant)
            return self._response(claimed)
        return await self._finish_turn(claimed, data, request, identity, grant)

    def cancel_turn(self, conversation_id: str, identity: PortalIdentity) -> ConversationResponse:
        current = self.store.get("conversation", conversation_id, subject_id=identity.subject_id)
        data = copy.deepcopy(current.record.data)
        if data.get("errorCode") == "turn-cancelled":
            return self._response(current)
        if data["status"] not in {"queued", "running"}:
            raise PortalStateConflict("There is no active turn to cancel.")
        data.update(status="failed", errorCode="turn-cancelled", workerLeaseOwner=None)
        data.pop("queuedTurn", None)
        return self._response(self.store.replace(current, data))

    def worker_available(self) -> bool:
        try:
            heartbeat = self.store.get("worker-heartbeat", "assistant")
        except KeyError:
            return False
        return datetime.now(UTC) - heartbeat.record.updated_at <= timedelta(minutes=3)

    def claim_worker(self, worker_id: str) -> bool:
        now = datetime.now(UTC)
        payload = {
            "service": "assistant",
            "owner": worker_id,
            "observedAt": now.isoformat(),
            "leaseUntil": (now + timedelta(seconds=90)).isoformat(),
        }
        try:
            current = self.store.get("worker-heartbeat", "assistant")
        except KeyError:
            try:
                self.store.create("worker-heartbeat", "assistant", "service", payload)
                return True
            except PortalStateConflict:
                current = self.store.get("worker-heartbeat", "assistant")
        previous = current.record.data
        if (
            previous.get("owner") not in {None, worker_id}
            and datetime.fromisoformat(previous["leaseUntil"]) > now
        ):
            return False
        self.store.replace(current, payload)
        return True

    def renew_worker(self, worker_id: str, *, release: bool = False) -> None:
        current = self.store.get("worker-heartbeat", "assistant")
        data = copy.deepcopy(current.record.data)
        if data.get("owner") != worker_id:
            raise PortalStateConflict("A different process owns the assistant worker lease.")
        now = datetime.now(UTC)
        if datetime.fromisoformat(data["leaseUntil"]) <= now:
            raise PortalStateConflict("The assistant worker lease expired; a fresh claim is required.")
        data.update(
            owner=None if release else worker_id,
            observedAt=now.isoformat(),
            leaseUntil=(now if release else now + timedelta(seconds=90)).isoformat(),
        )
        self.store.replace(current, data)

    def worker_authorization(self, data: dict[str, Any]) -> tuple[PortalIdentity, HubGrant]:
        if self.worker_sessions is None or self.worker_grants is None:
            raise PortalAuthorizationError("The assistant worker authorization services are unavailable.")
        current = self.store.get("conversation", data["workerKey"])
        stored = current.record.data
        if stored.get("workerLeaseOwner") != data["workerLeaseOwner"] or stored["status"] != "running":
            raise PortalAuthorizationError("The queued turn was cancelled or its ownership changed.")
        worker = self.store.get("worker-heartbeat", "assistant").record.data
        if worker.get("owner") != data["workerLeaseOwner"] or datetime.fromisoformat(
            worker["leaseUntil"]
        ) <= datetime.now(UTC):
            raise PortalAuthorizationError("The assistant worker lease is no longer current.")
        queued = stored["queuedTurn"]
        session = self.worker_sessions.get_session(queued["sessionReference"], datetime.now(UTC))
        if (
            session is None
            or session.subject_id != current.record.subject_id
            or session.tenant_id != self.store.scope.tenant_id
        ):
            raise PortalAuthorizationError("The accepted turn's session is expired or unavailable.")
        identity = identity_from_session(session)
        record = self.worker_grants.get(identity.subject_id, self.store.scope)
        if record is None:
            raise PortalAuthorizationError("The accepted turn's hub grant is no longer available.")
        authorize_portal_action(identity, record.grant, self.store.scope, PortalAction.ASSISTANT_USE)
        return identity, record.grant

    async def process_queued_turn(self, worker_id: str) -> bool:
        from starlette.concurrency import run_in_threadpool

        pending = await run_in_threadpool(self.store.pending_conversations, limit=1)
        if not pending:
            return False
        current = pending[0]
        data = copy.deepcopy(current.record.data)
        if data["status"] == "running":
            if datetime.fromisoformat(data["leaseUntil"]) > datetime.now(UTC):
                return False
            started = data.get("executionStarted", True)
            data.update(
                status="failed" if started else "queued",
                errorCode="turn-interrupted-review-drafts" if started else None,
                workerLeaseOwner=None,
            )
            if started:
                data.pop("queuedTurn", None)
            await run_in_threadpool(self.store.replace, current, data)
            return True
        data.update(
            status="running",
            workerKey=current.record.key,
            workerLeaseOwner=worker_id,
            executionStarted=False,
            leaseUntil=(datetime.now(UTC) + timedelta(seconds=_TURN_LEASE_SECONDS)).isoformat(),
        )
        claimed = await run_in_threadpool(self.store.replace, current, data)
        try:
            identity, grant = await run_in_threadpool(self.worker_authorization, data)
            queued = data["queuedTurn"]
            request = TurnRequest(request_id=queued["requestId"], message=queued["message"])
            if (
                str(request.request_id) != data["requestId"]
                or queued["requestHash"] != data["requestHash"]
                or hashlib.sha256(request.message.encode()).hexdigest() != data["requestHash"]
            ):
                raise ValueError("The queued request does not match its stored hash.")
        except AzureError:
            data.update(status="queued", workerLeaseOwner=None)
            await run_in_threadpool(self.store.replace, claimed, data)
            raise
        except (PortalAuthorizationError, ValueError, KeyError) as exc:
            logger.warning("Queued assistant authorization/request failed (%s).", type(exc).__name__)
            data.update(status="failed", errorCode="queued-turn-authorization-or-request-invalid")
            data.pop("queuedTurn", None)
            await run_in_threadpool(self.store.replace, claimed, data)
            return True
        data["executionStarted"] = True
        claimed = await run_in_threadpool(self.store.replace, claimed, data)
        try:
            await self._finish_turn(claimed, data, request, identity, grant)
        except (AssistantUnavailable, PortalStateConflict, AzureError) as exc:
            logger.error("Durable assistant turn did not complete (%s).", type(exc).__name__)
        return True

    async def _finish_background(
        self,
        claimed: VersionedDocument,
        data: dict[str, Any],
        request: TurnRequest,
        identity: PortalIdentity,
        grant: HubGrant,
    ) -> None:
        try:
            await self._finish_turn(claimed, data, request, identity, grant)
        except (AssistantUnavailable, PortalStateConflict, AzureError) as exc:
            logger.error("Deferred assistant turn could not complete (%s).", type(exc).__name__)

    async def _finish_turn(
        self,
        claimed: VersionedDocument,
        data: dict[str, Any],
        request: TurnRequest,
        identity: PortalIdentity,
        grant: HubGrant,
    ) -> ConversationResponse:
        import asyncio

        from starlette.concurrency import run_in_threadpool

        try:
            remaining = (identity.expires_at - datetime.now(UTC)).total_seconds()
            async with asyncio.timeout(min(_TURN_LEASE_SECONDS - 60, max(0, remaining))):
                text, state, drafts = await self.runner(data, request.message, identity, grant)
        except (APIError, AzureError, TimeoutError, OSError, RuntimeError, ValueError) as exc:
            logger.error("Hosted assistant turn failed (%s).", type(exc).__name__)
            data.update(status="failed", errorCode="assistant-turn-failed")
            data.pop("queuedTurn", None)
            await run_in_threadpool(self.store.replace, claimed, data)
            raise AssistantUnavailable(
                "The assistant could not complete this turn. Reload and retry."
            ) from exc
        data.update(
            status="idle",
            state=state,
            drafts=drafts,
        )
        data.pop("queuedTurn", None)
        data.pop("workerKey", None)
        data.pop("workerLeaseOwner", None)
        data.pop("executionStarted", None)
        now = datetime.now(UTC).isoformat()
        data["messages"].append(
            {
                "role": "assistant",
                "content": default_redactor().redact(text)[:32_000],
                "timestamp": now,
            }
        )
        result = await run_in_threadpool(self.store.replace, claimed, data)
        return self._response(result)

    async def _run(
        self, data: dict[str, Any], message: str, identity: PortalIdentity, grant: HubGrant
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        import asyncio

        from agent_framework import AgentSession
        from agent_framework.openai import OpenAIChatCompletionClient
        from openai import AsyncAzureOpenAI
        from starlette.concurrency import run_in_threadpool

        async def token() -> str:
            result = await run_in_threadpool(
                self.factory.credential.get_token, "https://cognitiveservices.azure.com/.default"
            )
            return result.token

        workspace_context = self.factory.workspace()
        root, workspace = await run_in_threadpool(workspace_context.__enter__)
        try:
            runtime = await run_in_threadpool(self.factory.create, root, workspace, identity, grant)
            drafts_root = runtime.store.directory.parent / "drafts"
            _restore_drafts(drafts_root, data["drafts"])
            context = ToolContext(runtime=runtime, drafts=DraftStore(drafts_root))
            from citadel_agent.portal.tools import hosted_registry

            registry = hosted_registry(
                build_registry(context),
                identity,
                grant,
                data=self.data_service,
                audit=runtime.audit,
                refresh_authorization=(lambda: self.worker_authorization(data))
                if data.get("workerKey")
                else None,
            )
            config = self.factory.config
            async with AsyncAzureOpenAI(
                azure_endpoint=config.model_endpoint,
                api_version=config.model_api_version,
                azure_ad_token_provider=token,
                timeout=90,
                max_retries=1,
            ) as sdk:
                client = OpenAIChatCompletionClient(model=config.model_deployment, async_client=sdk)
                from citadel_agent.agent import build_instructions

                instructions = (
                    build_instructions(runtime) + "\n\nHosted portal boundary:\n"
                    "Your effective user's persona comes from verified portal roles and an exact hub grant, "
                    "not from the service managed identity. Work only in the configured environment.\n"
                    "execute_change is intentionally unavailable. Never claim a chat message approves or "
                    "executes a change. After prepare_change, direct the user to Change review "
                    "below the chat. "
                    "Only its explicit, hash-bound approval and Submit pull request controls can act; "
                    "production requires an independent approver. No direct deployments are permitted.\n"
                    "For usage and cost analysis, prefer the FinOps page and its data-quality notes. "
                    "Do not invent prices or availability. Billable smoke tests are not exposed here."
                )
                bundle = create_agent(runtime, client=client, registry=registry, instructions=instructions)
                session = (
                    AgentSession.from_dict(data["state"]) if data["state"] else bundle.agent.create_session()
                )
                async with asyncio.timeout(300):
                    result = await bundle.agent.run(message, session=session, options={"max_tokens": 4000})
                if result.user_input_requests:
                    raise AssistantUnavailable("Use the stored change's approval and execution controls.")
                if not result.text:
                    raise AssistantUnavailable("The model returned an empty response.")
                text = result.text
                saved_drafts = (
                    {path.name: path.read_text(encoding="utf-8") for path in drafts_root.glob("d-*.yaml")}
                    if drafts_root.is_dir()
                    else {}
                )
                return text, default_redactor().redact_obj(session.to_dict()), saved_drafts
        finally:
            await run_in_threadpool(workspace_context.__exit__, None, None, None)

    def changes(self) -> list[ChangeResponse]:
        return [self.change(item.record.key) for item in self.store.list("hosted-change")]

    def change(self, change_id: str) -> ChangeResponse:
        current = self.store.get("hosted-change", change_id)
        data = copy.deepcopy(current.record.data)
        if data["executionStatus"] == "executing" and datetime.now(
            UTC
        ) - current.record.updated_at > timedelta(minutes=10):
            data["executionStatus"] = "needs-review"
            data["executionError"] = "submission-interrupted-effect-unknown"
            current = self.store.replace(current, data)
        change = ChangeSet.model_validate(data["change"])
        try:
            approval = self.factory.approvals.get(data["approvalId"]).record
        except KeyError:
            approval = None
        approval_status = (
            approval.status.value
            if approval is not None and approval.expires_at > datetime.now(UTC)
            else "expired"
        )
        status = data["status"]
        rotation = any(
            path.startswith(".citadel/rotations/") and '"phase": "rotate"' in text
            for path, text in data["files"].items()
        )
        return ChangeResponse(
            id=change.id,
            scope=self.store.scope,
            author_id=current.record.subject_id,
            summary=change.summary,
            preview=data["preview"],
            approval_hash=change.content_hash(),
            approval_id=approval.id if approval is not None else data["approvalId"],
            approval_status=approval_status,
            execution_status=data["executionStatus"],
            production=change.environment_class == "prod",
            confirmation_word="ROTATE" if rotation else None,
            pull_request_url=(status.get("pullRequest") or {}).get("url"),
        )

    def approve(
        self, change_id: str, decision: ChangeDecision, identity: PortalIdentity, grant: HubGrant
    ) -> ChangeResponse:
        current = self.store.get("hosted-change", change_id)
        data = current.record.data
        change = ChangeSet.model_validate(data["change"])
        if decision.approval_hash != change.content_hash():
            raise PortalStateConflict("The displayed change hash is no longer current.")
        if change.environment_class == "prod" and current.record.subject_id == identity.subject_id:
            raise AuthorizationError("Production changes require an independent approver.")
        with self.factory.workspace() as (root, workspace):
            runtime = self.factory.create(root, workspace, identity, grant)
            evidence = _evidence(change, data["preview"]).model_copy(
                update={
                    "repository_base_commit": workspace.base_commit(),
                    "admin_policy_hash": runtime.loaded.blob_sha,
                    "upstream_commit": runtime.lock.upstream.commit,
                }
            )
            self.factory.approvals.approve(
                data["approvalId"],
                identity=identity,
                grant=grant,
                requested_scope=self.store.scope,
                current_evidence=evidence,
                now=datetime.now(UTC),
            )
            runtime.audit.record("portal.change.approved", details={"change": change_id})
        return self.change(change_id)

    def execute(
        self, change_id: str, decision: ChangeDecision, identity: PortalIdentity, grant: HubGrant
    ) -> ChangeResponse:
        current = self.store.get("hosted-change", change_id)
        data = copy.deepcopy(current.record.data)
        change = ChangeSet.model_validate(data["change"])
        approval = self.factory.approvals.get(data["approvalId"]).record
        if (
            approval.status is not ApprovalStatus.APPROVED
            or approval.expires_at <= datetime.now(UTC)
            or approval.evidence != _evidence(change, data["preview"])
            or decision.approval_hash != change.content_hash()
        ):
            raise PortalStateConflict("An unexpired approval for this exact stored change is required.")
        if data["executionStatus"] != "prepared":
            if data["executionStatus"] in {"executing", "needs-review"}:
                raise PortalStateConflict(
                    "Submission is running or uncertain. Inspect PR/audit evidence; do not replay."
                )
            raise PortalStateConflict("This change was already submitted. Inspect its PR or audit status.")
        if not self.factory.github_token:
            raise AssistantUnavailable(
                "Configure the contracts GitHub credential in Key Vault before submission."
            )
        view = self.change(change_id)
        if view.confirmation_word and decision.confirmation != view.confirmation_word:
            raise ValueError(f"Type {view.confirmation_word} to acknowledge key replacement.")
        data["executionStatus"] = "executing"
        data["executionStartedAt"] = datetime.now(UTC).isoformat()
        self.store.replace(current, data)
        try:
            with self.factory.workspace() as (root, workspace):
                runtime = self.factory.create(root, workspace, identity, grant)
                result = runtime.engine().execute(change_id, decision.approval_hash)
                if "pullRequest" not in result:
                    raise ChangeError("No pull request was returned; review the recorded change status.")
        except (ChangeError, WorkspaceError, CommandError, GitProviderError, AzureError, OSError) as exc:
            latest = self.store.get("hosted-change", change_id)
            failed = copy.deepcopy(latest.record.data)
            failed["executionStatus"] = "needs-review"
            self.store.replace(latest, failed)
            logger.error("Hosted change execution failed (%s).", type(exc).__name__)
            raise AssistantUnavailable(
                "Submission failed. Inspect the audit/PR before preparing another change."
            ) from exc
        latest = self.store.get("hosted-change", change_id)
        completed = copy.deepcopy(latest.record.data)
        completed["executionStatus"] = "submitted"
        self.store.replace(latest, completed)
        return self.change(change_id)

    def _response(self, item: VersionedDocument) -> ConversationResponse:
        data = item.record.data
        return ConversationResponse(
            id=item.record.key,
            scope=item.record.scope,
            title=data["title"],
            messages=[ChatMessage.model_validate(message) for message in data["messages"]],
            status=data["status"],
            error_code=data.get("errorCode"),
            updated_at=item.record.updated_at,
        )


def _restore_drafts(root: Path, drafts: dict[str, str]) -> None:
    for name, content in drafts.items():
        relative = PurePosixPath(name)
        if (
            len(relative.parts) != 1
            or not name.startswith("d-")
            or relative.suffix != ".yaml"
            or "\\" in name
            or ":" in name
        ):
            raise ValueError("invalid saved draft path")
        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")


def _evidence(change: ChangeSet, preview: str) -> ApprovalEvidence:
    return ApprovalEvidence(
        change_hash=change.content_hash().removeprefix("sha256:"),
        repository_base_commit=change.repo_base_commit,
        admin_policy_hash=change.admin_policy_blob,
        upstream_commit=change.compat.agent_upstream_commit,
        preview_hash=hashlib.sha256(preview.encode()).hexdigest(),
    )
