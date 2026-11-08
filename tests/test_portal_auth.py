"""Entra validation and portal authorization deny untrusted identity and scope."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from citadel_agent.portal.auth import (
    EntraTokenSettings,
    EntraTokenValidator,
    PortalAuthenticationError,
    PortalIdentity,
)
from citadel_agent.portal.authz import (
    HubGrant,
    PortalAction,
    PortalAuthorizationError,
    PortalGrantPermission,
    authorize_portal_action,
)
from citadel_agent.portal.contracts import HubScope, PortalRole

TENANT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT_ID = "d94efdb2-f940-461a-b364-0fa281cd3af8"
AUDIENCE = "api://citadel-portal"
CLIENT_ID = "2d2d06d2-5c6e-4166-95ce-e5a63ca6441a"
USER_ID = "e1992d1d-a8d2-48d4-b7a6-80e287879d88"


@pytest.fixture
def token_tools():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    settings = EntraTokenSettings(
        tenant_id=TENANT_ID.upper(),
        audience=AUDIENCE,
        allowed_client_ids=frozenset({CLIENT_ID.upper()}),
    )
    validator = EntraTokenValidator(settings, signing_key_resolver=lambda _token: public_key)
    return validator, private_key, settings


def _token(private_key, settings, **claim_overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "aud": settings.audience,
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "iss": settings.issuer,
        "oid": USER_ID,
        "scp": "access_as_user profile",
        "tid": settings.tenant_id,
        "azp": CLIENT_ID,
        "roles": [PortalRole.VIEWER.value],
    }
    claims.update(claim_overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_valid_delegated_token_becomes_portal_identity(token_tools) -> None:
    validator, private_key, settings = token_tools
    token = _token(private_key, settings)
    claims = jwt.decode(
        token,
        private_key.public_key(),
        algorithms=["RS256"],
        audience=settings.audience,
        issuer=settings.issuer,
    )
    identity = validator.validate(token)

    assert identity == PortalIdentity(
        subject_id=USER_ID,
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        expires_at=datetime.fromtimestamp(claims["exp"], UTC),
        scopes=frozenset({"access_as_user", "profile"}),
        roles=frozenset({PortalRole.VIEWER}),
    )


def test_identity_metadata_uses_a_fixed_tenant_authority(token_tools) -> None:
    _, _, settings = token_tools
    assert settings.issuer == f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
    assert settings.jwks_uri == (f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys")


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "api://another-api"},
        {"iss": f"https://login.microsoftonline.com/{OTHER_TENANT_ID}/v2.0"},
        {"tid": OTHER_TENANT_ID},
        {"azp": "9c53be8b-872b-43ae-958f-ef64e1bb2033"},
        {"scp": "profile"},
        {"scp": None},
        {"oid": "not-a-guid"},
    ],
)
def test_invalid_audience_tenant_client_or_delegation_is_denied(token_tools, claims) -> None:
    validator, private_key, settings = token_tools
    with pytest.raises(PortalAuthenticationError):
        validator.validate(_token(private_key, settings, **claims))


def test_expired_access_token_is_denied(token_tools) -> None:
    validator, private_key, settings = token_tools
    with pytest.raises(PortalAuthenticationError):
        validator.validate(_token(private_key, settings, exp=datetime.now(UTC) - timedelta(minutes=1)))


def test_not_yet_valid_access_token_is_denied(token_tools) -> None:
    validator, private_key, settings = token_tools
    with pytest.raises(PortalAuthenticationError):
        validator.validate(_token(private_key, settings, nbf=datetime.now(UTC) + timedelta(minutes=1)))


def test_signature_and_algorithm_must_be_trusted(token_tools) -> None:
    validator, _, settings = token_tools
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(PortalAuthenticationError):
        validator.validate(_token(other_key, settings))

    unsigned = jwt.encode(
        {"aud": settings.audience},
        "not-a-signing-key-that-is-at-least-32-bytes",
        algorithm="HS256",
    )
    with pytest.raises(PortalAuthenticationError):
        validator.validate(unsigned)


def test_only_known_signed_roles_are_exposed(token_tools) -> None:
    validator, private_key, settings = token_tools
    identity = validator.validate(
        _token(
            private_key,
            settings,
            roles=[PortalRole.OPERATOR.value, "Platform.SuperAdmin"],
        )
    )
    assert identity.roles == frozenset({PortalRole.OPERATOR})

    with pytest.raises(PortalAuthenticationError):
        validator.validate(_token(private_key, settings, roles="Platform.Operator"))


def _scope(hub_id: str = "production-hub") -> HubScope:
    return HubScope(
        tenant_id=TENANT_ID,
        hub_id=hub_id,
        environment="prod",
        subscription_id="22222222-2222-4222-8222-222222222222",
    )


def _identity(role: PortalRole = PortalRole.VIEWER, subject_id: str = USER_ID) -> PortalIdentity:
    return PortalIdentity(
        subject_id=subject_id,
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        scopes=frozenset({"access_as_user"}),
        roles=frozenset({role}),
    )


def test_viewer_can_read_only_inside_the_exact_granted_scope() -> None:
    scope = _scope()
    grant = HubGrant(subject_id=USER_ID, scope=scope)

    authorize_portal_action(_identity(), grant, scope, PortalAction.GATEWAY_READ)
    with pytest.raises(PortalAuthorizationError):
        authorize_portal_action(_identity(), grant, scope, PortalAction.CHANGE_DRAFT)
    with pytest.raises(PortalAuthorizationError):
        authorize_portal_action(_identity(), grant, _scope("other-hub"), PortalAction.GATEWAY_READ)


@pytest.mark.parametrize(
    "subject_id,tenant_id,scope",
    [
        ("10c669b5-15ea-48b0-9e11-232a29be4d74", TENANT_ID, _scope()),
        (USER_ID, OTHER_TENANT_ID, _scope()),
        (USER_ID, TENANT_ID, _scope("other-hub")),
    ],
)
def test_identity_and_grant_must_match_the_requested_scope(subject_id, tenant_id, scope) -> None:
    identity = PortalIdentity(
        subject_id=USER_ID,
        tenant_id=tenant_id,
        client_id=CLIENT_ID,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        scopes=frozenset({"access_as_user"}),
        roles=frozenset({PortalRole.OPERATOR}),
    )
    grant = HubGrant(subject_id=subject_id, scope=_scope())
    with pytest.raises(PortalAuthorizationError):
        authorize_portal_action(identity, grant, scope, PortalAction.GATEWAY_READ)


def test_sensitive_access_requires_an_explicit_grant_even_for_role_holders() -> None:
    scope = _scope()
    analyst = _identity(PortalRole.FINOPS_ANALYST)
    grant = HubGrant(subject_id=USER_ID, scope=scope)

    with pytest.raises(PortalAuthorizationError):
        authorize_portal_action(analyst, grant, scope, PortalAction.FINOPS_READ)

    authorized = grant.model_copy(update={"permissions": frozenset({PortalGrantPermission.FINOPS_DATA})})
    authorize_portal_action(analyst, authorized, scope, PortalAction.FINOPS_READ)
    with pytest.raises(PortalAuthorizationError):
        authorize_portal_action(analyst, authorized, scope, PortalAction.EXPORT_CREATE)
