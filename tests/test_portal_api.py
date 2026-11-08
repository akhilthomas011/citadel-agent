"""Private API job status is restricted to the owning user and authorized hub scope."""

from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from citadel_agent.portal.api import create_agent_app
from citadel_agent.portal.auth import EntraServiceTokenValidator, EntraTokenSettings, PortalRole
from citadel_agent.portal.authz import HubGrant, PortalGrantPermission
from citadel_agent.portal.contracts import HubScope
from citadel_agent.portal.grants import VersionedHubGrant
from citadel_agent.portal.jobs import (
    PortalJobKind,
    PortalJobRecord,
    PortalJobStatus,
    VersionedPortalJob,
)
from citadel_agent.portal.sessions import PortalSessionRecord

TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "e1992d1d-a8d2-48d4-b7a6-80e287879d88"
OTHER_USER_ID = "10c669b5-15ea-48b0-9e11-232a29be4d74"
BFF_CLIENT_ID = "2d2d06d2-5c6e-4166-95ce-e5a63ca6441a"
NOW = datetime.now(UTC)


class MemorySessionStore:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.session = PortalSessionRecord(
            id="a" * 64,
            tenant_id=TENANT_ID,
            subject_id=USER_ID,
            client_id=BFF_CLIENT_ID,
            roles=frozenset({PortalRole.VIEWER, PortalRole.FINOPS_ANALYST}),
            scopes=frozenset({"access_as_user"}),
            csrf_token="c" * 43,
            created_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=10),
            ttl=600,
        )

    def get_session(self, record_id, now):
        return self.session if record_id == self.session.id and now < self.session.expires_at else None

    def create_auth_flow(self, record):
        raise NotImplementedError

    def consume_auth_flow(self, record_id, now):
        raise NotImplementedError

    def create_session(self, record):
        raise NotImplementedError

    def delete_session(self, record_id):
        raise NotImplementedError


class MemoryGrantStore:
    def __init__(self, grant: HubGrant | None) -> None:
        self.grant = grant

    def get(self, subject_id: str, scope: HubScope) -> VersionedHubGrant | None:
        if self.grant is None or self.grant.subject_id != subject_id or self.grant.scope != scope:
            return None
        return VersionedHubGrant(grant=self.grant, etag="grant-etag")


class MemoryJobStore:
    def __init__(self, job: PortalJobRecord) -> None:
        self.job = job

    def get(self, job_id: str) -> VersionedPortalJob:
        if job_id != self.job.id:
            raise KeyError(job_id)
        return VersionedPortalJob(record=self.job, etag="job-etag", created=False)

    def submit(self, **kwargs):
        raise NotImplementedError

    def transition(self, *args, **kwargs):
        raise NotImplementedError


def _scope() -> HubScope:
    return HubScope(
        tenant_id=TENANT_ID,
        hub_id="citadel-production",
        environment="prod",
        subscription_id="22222222-2222-4222-8222-222222222222",
    )


def _job(*, subject_id: str = USER_ID, kind: PortalJobKind = PortalJobKind.ASSISTANT_TURN):
    return PortalJobRecord(
        id="b" * 64,
        job_kind=kind,
        scope=_scope(),
        subject_id=subject_id,
        idempotency_key_hash="d" * 64,
        request_hash="e" * 64,
        status=PortalJobStatus.SUCCEEDED,
        created_at=NOW - timedelta(minutes=1),
        updated_at=NOW,
        completed_at=NOW,
        result_refs={"private-result": "result-store-id"},
        ttl=3600,
    )


def _client(
    *,
    roles=(PortalRole.VIEWER, PortalRole.FINOPS_ANALYST),
    kind=PortalJobKind.ASSISTANT_TURN,
    grant_enabled: bool = True,
    job_subject_id: str = USER_ID,
):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = EntraTokenSettings(
        tenant_id=TENANT_ID,
        audience="api://citadel-agent",
        allowed_client_ids=frozenset({BFF_CLIENT_ID}),
    )
    validator = EntraServiceTokenValidator(
        settings,
        signing_key_resolver=lambda _token: private_key.public_key(),
    )
    token = jwt.encode(
        {
            "aud": settings.audience,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "iat": datetime.now(UTC),
            "iss": settings.issuer,
            "oid": "0a563df8-9c61-4d73-90aa-36c103931fe9",
            "roles": ["Portal.InvokeAgent"],
            "tid": settings.tenant_id,
            "azp": BFF_CLIENT_ID,
        },
        private_key,
        algorithm="RS256",
    )
    session_store = MemorySessionStore()
    session_store.session = session_store.session.model_copy(update={"roles": frozenset(roles)})
    grant = (
        HubGrant(
            subject_id=USER_ID,
            scope=_scope(),
            permissions=frozenset({PortalGrantPermission.EXPORTS}),
        )
        if grant_enabled
        else None
    )
    app = create_agent_app(
        service_token_validator=validator,
        session_store=session_store,
        grant_store=MemoryGrantStore(grant),
        job_store=MemoryJobStore(_job(subject_id=job_subject_id, kind=kind)),
    )
    return TestClient(app), token, session_store.session.id


def test_job_status_requires_valid_private_service_and_session_context() -> None:
    client, token, session_id = _client()

    response = client.get(
        f"/internal/jobs/{'b' * 64}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Portal-Session-Ref": session_id,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["scope"]["hubId"] == "citadel-production"
    assert "resultRefs" not in response.json()


def test_job_status_denies_users_without_an_exact_scope_grant() -> None:
    client, token, session_id = _client(grant_enabled=False)
    response = client.get(
        f"/internal/jobs/{'b' * 64}",
        headers={"Authorization": f"Bearer {token}", "X-Portal-Session-Ref": session_id},
    )
    assert response.status_code == 403


def test_job_status_hides_another_users_job() -> None:
    client, token, session_id = _client(job_subject_id=OTHER_USER_ID)
    response = client.get(
        f"/internal/jobs/{'b' * 64}",
        headers={"Authorization": f"Bearer {token}", "X-Portal-Session-Ref": session_id},
    )
    assert response.status_code == 404


def test_export_job_requires_finops_role_and_export_grant() -> None:
    client, token, session_id = _client(
        roles=(PortalRole.VIEWER,),
        kind=PortalJobKind.ANALYTICS_EXPORT,
    )
    response = client.get(
        f"/internal/jobs/{'b' * 64}",
        headers={"Authorization": f"Bearer {token}", "X-Portal-Session-Ref": session_id},
    )
    assert response.status_code == 403
