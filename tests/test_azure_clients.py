# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the Azure client layer (fakes and HTTP mocks; no Azure calls)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from citadel_agent.azure_clients import arm, bicep, deployments
from citadel_agent.azure_clients.apim import ApimClient
from citadel_agent.azure_clients.cosmos import CosmosUsageReader
from citadel_agent.azure_clients.credentials import decode_token_claims, describe_principal
from citadel_agent.azure_clients.keyvault import KeyVaultAccessError, KeyVaultReader
from citadel_agent.safety.redaction import REDACTED, Redactor


def _jwt(claims: dict[str, Any]) -> str:
    def enc(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class FakeCredential:
    def __init__(self, claims: dict[str, Any] | None = None) -> None:
        self.token = _jwt(claims or {"oid": "oid-1", "tid": "tid-1", "upn": "dev@contoso.com"})

    def get_token(self, *scopes: str, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(token=self.token, expires_on=9_999_999_999)


# Credentials -----------------------------------------------------------------------------------


def test_describe_principal_reads_claims() -> None:
    info = describe_principal(FakeCredential())
    assert info.object_id == "oid-1"
    assert info.principal_type == "User"
    assert info.label == "dev@contoso.com"


def test_decode_token_claims_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        decode_token_claims("not-a-jwt")


# ARM permissions and REST ----------------------------------------------------------------------


def test_permissions_are_evaluated_per_role_assignment() -> None:
    contributor = {"actions": ["*"], "notActions": ["Microsoft.Authorization/*/Write"]}
    user_access_admin = {"actions": ["Microsoft.Authorization/*"], "notActions": []}
    perms = arm.merge_permissions([contributor, user_access_admin])
    assert perms.allows("Microsoft.Authorization/roleAssignments/write")
    assert perms.allows("Microsoft.ApiManagement/service/products/write")

    contributor_only = arm.merge_permissions([contributor])
    assert not contributor_only.allows("Microsoft.Authorization/roleAssignments/write")


def test_data_actions_are_evaluated() -> None:
    perms = arm.merge_permissions([{"dataActions": ["Microsoft.KeyVault/vaults/secrets/*"]}])
    assert perms.allows_data("Microsoft.KeyVault/vaults/secrets/getSecret/action")
    assert not perms.allows("Microsoft.KeyVault/vaults/write")


@respx.mock
def test_list_follows_next_link_and_sends_bearer_token() -> None:
    first = respx.get("https://management.azure.com/subscriptions/s/resourceGroups/rg/resources").mock(
        return_value=httpx.Response(
            200, json={"value": [{"name": "a"}], "nextLink": "https://management.azure.com/next?page=2"}
        )
    )
    respx.get("https://management.azure.com/next").mock(
        return_value=httpx.Response(200, json={"value": [{"name": "b"}]})
    )
    client = arm.ArmClient(FakeCredential())

    names = [r["name"] for r in client.list_resources("s", "rg")]

    assert names == ["a", "b"]
    assert first.calls[0].request.headers["Authorization"].startswith("Bearer ")


@respx.mock
def test_arm_errors_carry_status_and_code() -> None:
    respx.get("https://management.azure.com/x").mock(
        return_value=httpx.Response(403, json={"error": {"code": "AuthorizationFailed", "message": "denied"}})
    )
    with pytest.raises(arm.ArmError) as excinfo:
        arm.ArmClient(FakeCredential()).get("/x", "2021-04-01")
    assert excinfo.value.status_code == 403
    assert excinfo.value.code == "AuthorizationFailed"


# Bicep -----------------------------------------------------------------------------------------


def test_parse_diagnostics_extracts_errors_and_warnings() -> None:
    output = (
        "C:\\x\\main.bicep(21,7) : Warning no-unused-params: Parameter is unused.\n"
        "C:\\x\\main.bicep(3,1) : Error BCP018: Expected the ':' character.\n"
        "noise line\n"
    )
    diagnostics = bicep.parse_diagnostics(output)
    assert [(d.level, d.code, d.line) for d in diagnostics] == [
        ("Warning", "no-unused-params", 21),
        ("Error", "BCP018", 3),
    ]


def test_resolve_using(tmp_path: Path) -> None:
    (tmp_path / "contracts").mkdir()
    params = tmp_path / "contracts" / "main.bicepparam"
    params.write_text("using '../main.bicep'\nparam x = 1\n", encoding="utf-8")
    assert bicep.resolve_using(params) == (tmp_path / "main.bicep").resolve()


def test_canonical_hash_is_order_independent() -> None:
    assert bicep.canonical_hash({"a": 1, "b": [1, 2]}) == bicep.canonical_hash({"b": [1, 2], "a": 1})
    assert bicep.canonical_hash({"a": 1}).startswith("sha256:")


@pytest.mark.az
def test_compile_deployment_with_real_bicep_cli(tmp_path: Path) -> None:
    (tmp_path / "main.bicep").write_text(
        "targetScope = 'subscription'\nparam greeting string\noutput echo string = greeting\n",
        encoding="utf-8",
    )
    params = tmp_path / "main.bicepparam"
    params.write_text("using 'main.bicep'\nparam greeting = 'hello ✓'\n", encoding="utf-8")

    compiled = bicep.compile_deployment(params)

    assert compiled.parameters["greeting"]["value"] == "hello ✓"
    assert "outputs" in compiled.template
    assert compiled.template_hash.startswith("sha256:")


@pytest.mark.az
def test_compile_errors_are_reported(tmp_path: Path) -> None:
    (tmp_path / "main.bicep").write_text("param x string = \n", encoding="utf-8")
    with pytest.raises(bicep.BicepCompileError) as excinfo:
        bicep.compile_template(tmp_path / "main.bicep")
    assert any(d.level == "Error" for d in excinfo.value.diagnostics)


# Deployments -----------------------------------------------------------------------------------

PRODUCT_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim/products/p"
WHAT_IF_RESULT = {
    "status": "Succeeded",
    "changes": [
        {
            "resourceId": PRODUCT_ID,
            "changeType": "Modify",
            "delta": [
                {
                    "path": "properties",
                    "propertyChangeType": "Modify",
                    "children": [{"path": "state", "propertyChangeType": "Modify"}],
                }
            ],
        },
        {"resourceId": "/x/unchanged", "changeType": "NoChange"},
        {"resourceId": "/x/new", "changeType": "Create"},
    ],
}


def test_parse_what_if_flattens_property_changes() -> None:
    report = deployments.parse_what_if_result(WHAT_IF_RESULT)
    assert report.counts() == {"Modify": 1, "NoChange": 1, "Create": 1}
    assert report.changes[0].property_changes == ("properties.state (Modify)",)
    assert report.has_changes


def test_fingerprint_ignores_no_change_entries_and_order() -> None:
    report = deployments.parse_what_if_result(WHAT_IF_RESULT)
    first, second, third = WHAT_IF_RESULT["changes"]
    reordered = dict(WHAT_IF_RESULT, changes=[second, first, third])
    without_nochange = dict(WHAT_IF_RESULT, changes=[first, third])
    assert report.fingerprint() == deployments.parse_what_if_result(reordered).fingerprint()
    assert report.fingerprint() == deployments.parse_what_if_result(without_nochange).fingerprint()


# The shape current SDK models return from as_dict() (REST wire format, verified against ARM).
WIRE_WHAT_IF_RESULT: dict[str, Any] = {
    "status": "Succeeded",
    "properties": {
        "correlationId": "c-1",
        "changes": [
            {"resourceId": PRODUCT_ID, "changeType": "NoChange"},
            {"resourceId": f"{PRODUCT_ID}/apis/unified-ai-api", "changeType": "Create"},
            {
                "resourceId": f"{PRODUCT_ID}/policies/policy",
                "changeType": "Modify",
                "delta": [{"path": "properties.value", "propertyChangeType": "Modify"}],
            },
        ],
        "potentialChanges": [{"resourceId": "/x/nested", "changeType": "Create"}],
        "diagnostics": [
            {
                "level": "Warning",
                "code": "NestedDeploymentShortCircuited",
                "message": "Nested deployment skipped",
                "target": "/x/dep",
            }
        ],
    },
}


def test_parse_what_if_reads_the_rest_wire_shape() -> None:
    report = deployments.parse_what_if_result(WIRE_WHAT_IF_RESULT)
    assert report.counts() == {"NoChange": 1, "Create": 1, "Modify": 1, "Create (potential)": 1}
    assert report.changes[2].property_changes == ("properties.value (Modify)",)
    assert report.diagnostics == (
        "Warning NestedDeploymentShortCircuited: Nested deployment skipped (/x/dep)",
    )
    assert report.has_changes


def test_parse_what_if_accepts_sdk_models() -> None:
    model = SimpleNamespace(as_dict=lambda: WIRE_WHAT_IF_RESULT)
    assert deployments.parse_what_if_result(model).counts()["Create"] == 1


def test_fingerprint_detects_changed_potential_changes() -> None:
    report = deployments.parse_what_if_result(WIRE_WHAT_IF_RESULT)
    properties = dict(WIRE_WHAT_IF_RESULT["properties"], potentialChanges=[])
    without = deployments.parse_what_if_result({"status": "Succeeded", "properties": properties})
    assert report.fingerprint() != without.fingerprint()
    assert report.fingerprint() != deployments.WhatIfReport(status="Succeeded").fingerprint()


def test_deployment_target_validation() -> None:
    with pytest.raises(ValueError):
        deployments.DeploymentTarget(scope="subscription", subscription_id="s")
    with pytest.raises(ValueError):
        deployments.DeploymentTarget(scope="resourceGroup", subscription_id="s")
    target = deployments.DeploymentTarget(scope="resourceGroup", subscription_id="s", resource_group="rg")
    assert target.scope_id == "/subscriptions/s/resourceGroups/rg"


@pytest.mark.parametrize("name", ["", "a" * 65, "bad name", "semi;colon"])
def test_invalid_deployment_names(name: str) -> None:
    with pytest.raises(deployments.DeploymentNameError):
        deployments.validate_deployment_name(name)


class _Poller:
    def __init__(self, result: Any) -> None:
        self._result = result

    def result(self, timeout: float | None = None) -> Any:
        return self._result


class FakeDeployments:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def begin_what_if_at_subscription_scope(self, name: str, body: dict[str, Any]) -> _Poller:
        self.calls.append(("what_if_sub", (name, body)))
        return _Poller(WHAT_IF_RESULT)

    def begin_create_or_update(self, rg: str, name: str, body: dict[str, Any]) -> _Poller:
        self.calls.append(("create_rg", (rg, name, body)))
        return _Poller(
            {
                "name": name,
                "properties": {
                    "provisioningState": "Succeeded",
                    "outputs": {"activeKeySlot": {"value": "primary"}},
                },
            }
        )


def _compiled(tmp_path: Path) -> bicep.CompiledDeployment:
    return bicep.CompiledDeployment(
        params_file=tmp_path / "p.bicepparam",
        template_file=tmp_path / "t.bicep",
        template={"t": 1},
        parameters={"p": {"value": 1}},
    )


def test_runner_uses_scope_specific_operations(tmp_path: Path) -> None:
    fake = FakeDeployments()
    runner = deployments.DeploymentRunner(FakeCredential(), "s", client=SimpleNamespace(deployments=fake))

    report = runner.what_if(
        deployments.DeploymentTarget(scope="subscription", subscription_id="s", location="swedencentral"),
        "wi-1",
        _compiled(tmp_path),
    )
    record = runner.deploy(
        deployments.DeploymentTarget(scope="resourceGroup", subscription_id="s", resource_group="rg"),
        "dep-1",
        _compiled(tmp_path),
    )

    kind, (name, body) = fake.calls[0]
    assert kind == "what_if_sub" and name == "wi-1"
    assert body["location"] == "swedencentral"
    assert body["properties"]["whatIfSettings"]["resultFormat"] == "FullResourcePayloads"
    assert report.counts()["Create"] == 1
    assert fake.calls[1][0] == "create_rg"
    assert "location" not in fake.calls[1][1][2]
    assert record.succeeded and record.output("activeKeySlot") == "primary"


class _FailingPoller:
    def result(self, timeout: float | None = None) -> Any:
        raise HttpResponseError(message="Deployment failed")


class FailingDeployments:
    def begin_create_or_update_at_subscription_scope(self, name: str, body: dict[str, Any]) -> _FailingPoller:
        return _FailingPoller()

    def get_at_subscription_scope(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "properties": {
                "provisioningState": "Failed",
                "error": {
                    "code": "DeploymentFailed",
                    "message": "At least one resource deployment operation failed.",
                    "details": [{"code": "ValidationError", "message": "Product name is invalid"}],
                },
            },
        }


def test_failed_deployment_returns_a_record_with_nested_errors(tmp_path: Path) -> None:
    runner = deployments.DeploymentRunner(
        FakeCredential(), "s", client=SimpleNamespace(deployments=FailingDeployments())
    )
    record = runner.deploy(
        deployments.DeploymentTarget(scope="subscription", subscription_id="s", location="swedencentral"),
        "dep-1",
        _compiled(tmp_path),
    )
    assert not record.succeeded and record.provisioning_state == "Failed"
    assert record.error is not None
    assert "DeploymentFailed" in record.error
    assert "ValidationError: Product name is invalid" in record.error


# APIM, Key Vault and Cosmos ---------------------------------------------------------------------


def _fake_apim() -> SimpleNamespace:
    return SimpleNamespace(
        api_management_service=SimpleNamespace(
            get=lambda rg, svc: SimpleNamespace(
                name=svc,
                gateway_url="https://apim.azure-api.net",
                sku=SimpleNamespace(name="StandardV2"),
                location="swedencentral",
                identity=SimpleNamespace(user_assigned_identities={"/id/uami": object()}),
            )
        ),
        api=SimpleNamespace(
            list_by_service=lambda rg, svc: [
                SimpleNamespace(
                    name="universal-llm-api",
                    display_name="Universal LLM API",
                    path="models",
                    api_type=None,
                    subscription_required=True,
                    is_current=True,
                ),
                SimpleNamespace(
                    name="old-rev",
                    display_name="Old",
                    path="x",
                    api_type=None,
                    subscription_required=True,
                    is_current=False,
                ),
            ]
        ),
        subscription=SimpleNamespace(
            list=lambda rg, svc: [
                SimpleNamespace(
                    name="LLM-HR-Chat-DEV-SUB-01",
                    display_name="d",
                    scope="/subscriptions/s/.../products/LLM-HR-Chat-DEV",
                    state="active",
                )
            ],
            list_secrets=lambda rg, svc, sid: SimpleNamespace(
                primary_key="primary-key-value-123", secondary_key="secondary-key-value-456"
            ),
        ),
        named_value=SimpleNamespace(
            get=lambda rg, svc, nv: SimpleNamespace(secret=nv == "secret-nv", value="the-value"),
        ),
    )


def test_apim_client_lists_current_apis_and_parses_product_scope() -> None:
    client = ApimClient(FakeCredential(), "s", "rg", "apim", client=_fake_apim(), redactor=Redactor())
    assert client.service().user_assigned_identity_ids == ("/id/uami",)
    assert [a.name for a in client.list_apis()] == ["universal-llm-api"]
    assert client.list_subscriptions()[0].product_id == "LLM-HR-Chat-DEV"


def test_apim_keys_are_registered_for_redaction_and_secret_named_values_hidden() -> None:
    redactor = Redactor()
    client = ApimClient(FakeCredential(), "s", "rg", "apim", client=_fake_apim(), redactor=redactor)
    primary, _ = client.subscription_keys_in_memory("sub")
    assert redactor.redact(f"leak {primary}") == f"leak {REDACTED}"
    assert client.get_named_value("secret-nv") is None
    assert client.get_named_value("plain-nv") == "the-value"


def test_key_vault_reader_registers_secret_values() -> None:
    redactor = Redactor()
    fake = SimpleNamespace(
        list_properties_of_secrets=lambda: [
            SimpleNamespace(name="b", enabled=True),
            SimpleNamespace(name="a", enabled=False),
        ],
        get_secret=lambda name: SimpleNamespace(value="kv-secret-value-789"),
    )
    reader = KeyVaultReader(FakeCredential(), "https://kv.vault.azure.net/", client=fake, redactor=redactor)
    assert reader.list_secret_names() == ["b"]
    value = reader.get_secret_in_memory("b")
    assert redactor.redact(value) == REDACTED


def _failing_reader(error: Exception) -> KeyVaultReader:
    def fail(*_: Any) -> Any:
        raise error

    fake = SimpleNamespace(get_secret=fail, list_properties_of_secrets=fail)
    return KeyVaultReader(FakeCredential(), "https://kv.vault.azure.net/", client=fake, redactor=Redactor())


def test_key_vault_firewall_denial_is_explained() -> None:
    reader = _failing_reader(
        HttpResponseError(
            message=(
                "(Forbidden) Client address is not authorized and caller is not a trusted service.\n"
                'Client address: 203.0.113.7\nInner error: {"code": "ForbiddenByFirewall"}'
            )
        )
    )
    with pytest.raises(KeyVaultAccessError, match=r"203\.0\.113\.7 \(network rules\)"):
        reader.get_secret_in_memory("x")
    with pytest.raises(KeyVaultAccessError, match="network rules"):
        reader.list_secret_names()


def test_key_vault_rbac_denial_and_missing_secrets() -> None:
    reader = _failing_reader(HttpResponseError(message='Inner error: {"code": "ForbiddenByRbac"}'))
    with pytest.raises(KeyVaultAccessError, match="Key Vault Secrets User"):
        reader.get_secret_in_memory("x")
    assert _failing_reader(ResourceNotFoundError(message="missing")).secret_exists("x") is False


def test_cosmos_discovers_usage_database_and_binds_parameters() -> None:
    captured: dict[str, Any] = {}

    def query_items(
        query: str, parameters: list[dict[str, Any]], enable_cross_partition_query: bool
    ) -> list[dict[str, Any]]:
        captured["parameters"] = parameters
        return [{"totalTokens": "10"}]

    container = SimpleNamespace(query_items=query_items)
    databases = {
        "other": SimpleNamespace(list_containers=lambda: [{"id": "x"}]),
        "usage-db": SimpleNamespace(
            list_containers=lambda: [{"id": "llm-usage-container"}],
            get_container_client=lambda name: container,
        ),
    }
    fake = SimpleNamespace(
        list_databases=lambda: [{"id": "other"}, {"id": "usage-db"}],
        get_database_client=lambda name: databases[name],
    )
    reader = CosmosUsageReader(FakeCredential(), "https://acct.documents.azure.com:443/", client=fake)

    assert reader.discover_usage_database() == "usage-db"
    rows = reader.query(
        "usage-db", "llm-usage-container", "SELECT * FROM c WHERE c.productName = @product", {"product": "P"}
    )
    assert rows == [{"totalTokens": "10"}]
    assert captured["parameters"] == [{"name": "@product", "value": "P"}]
