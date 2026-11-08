"""Verify compiled infrastructure properties rather than relying on source-text assertions."""

import copy
import json
from pathlib import Path

import pytest

from citadel_agent.proc import CommandError, resolve_executable, run_command
from test_hosted_delivery import module

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    try:
        resolve_executable("bicep")
    except CommandError:
        pytest.skip("Bicep CLI is required for compiled hosted-infrastructure tests")
    path = tmp_path_factory.mktemp("hosted-infra") / "main.json"
    run_command("bicep", ["build", str(ROOT / "infra" / "main.bicep"), "--outfile", str(path)], timeout=120)
    return json.loads(path.read_text())


def resources(template):
    items = template.get("resources", {})
    for resource in items.values() if isinstance(items, dict) else items:
        yield resource
        nested = resource.get("properties", {}).get("template")
        if nested is not None:
            yield from resources(nested)


@pytest.mark.az
def test_compiled_hosting_is_private_passwordless_and_scoped(compiled):
    all_resources = list(resources(compiled))
    state = next(item for item in all_resources if item["type"] == "Microsoft.DocumentDB/databaseAccounts")
    assert state["properties"]["publicNetworkAccess"] == "Disabled"
    assert state["properties"]["disableLocalAuth"] is True
    assert state["properties"]["minimalTlsVersion"] == "Tls12"
    assert state["properties"]["backupPolicy"]["type"] == "Continuous"
    assert state["properties"]["enableMultipleWriteLocations"] is False
    environment = next(item for item in all_resources if item["type"] == "Microsoft.App/managedEnvironments")
    assert environment["properties"]["vnetConfiguration"]["internal"] is True
    assert environment["properties"]["appLogsConfiguration"] == {"destination": "azure-monitor"}
    agent = next(item for item in all_resources if item["type"] == "Microsoft.App/containerApps")
    ingress = agent["properties"]["configuration"]["ingress"]
    assert ingress["external"] is True
    assert ingress["allowInsecure"] is False
    assert ingress["targetPort"] == 8000
    assert agent["identity"]["type"] == "UserAssigned"
    probes = agent["properties"]["template"]["containers"][0]["probes"]
    assert {probe["type"] for probe in probes} == {"Startup", "Readiness", "Liveness"}
    assert agent["properties"]["template"]["scale"]["minReplicas"] >= 2
    assert not any(item["type"] == "Microsoft.Web/sites" for item in all_resources)
    assert not any(
        item["type"]
        in {
            "Microsoft.KeyVault/vaults",
            "Microsoft.ApiManagement/service",
            "Microsoft.CognitiveServices/accounts",
        }
        for item in all_resources
    )
    text = json.dumps(compiled).lower()
    assert "listkeys(" not in text and "sharedkey" not in text
    assignments = [item for item in all_resources if item["type"].endswith("/sqlRoleAssignments")]
    assert assignments
    assert all("/dbs/" in item["properties"]["scope"] for item in assignments)


@pytest.mark.az
def test_private_network_has_dedicated_subnets_and_explicit_outbound_path(compiled):
    all_resources = list(resources(compiled))
    vnet = next(item for item in all_resources if item["type"] == "Microsoft.Network/virtualNetworks")
    subnets = {item["name"]: item["properties"] for item in vnet["properties"]["subnets"]}
    assert subnets["agent"]["delegations"][0]["properties"]["serviceName"] == "Microsoft.App/environments"
    assert subnets["bff"]["delegations"][0]["properties"]["serviceName"] == "Microsoft.Web/serverFarms"
    assert not subnets["endpoints"].get("delegations")
    assert subnets["endpoints"]["privateEndpointNetworkPolicies"] == "Enabled"
    assert all(value["defaultOutboundAccess"] is False for value in subnets.values())
    assert subnets["agent"]["natGateway"]["id"]
    assert subnets["bff"]["natGateway"]["id"]
    assert any(item["type"] == "Microsoft.Network/natGateways" for item in all_resources)


@pytest.mark.az
def test_analytics_job_is_private_finite_and_has_no_session_model_or_vault_secrets(compiled):
    all_resources = list(resources(compiled))
    job = next(
        item
        for item in all_resources
        if item["type"] == "Microsoft.App/jobs"
        and item["properties"]["template"]["containers"][0]["args"] == ["analytics"]
    )
    assert job["identity"]["type"] == "UserAssigned"
    config = job["properties"]["configuration"]
    assert config["triggerType"] == "Schedule"
    assert config["replicaTimeout"] == 1800
    assert config["replicaRetryLimit"] == 0
    assert config["scheduleTriggerConfig"] == {
        "cronExpression": "*/5 * * * *",
        "parallelism": 1,
        "replicaCompletionCount": 1,
    }
    assert "ingress" not in config
    assert "secrets" not in config
    container = job["properties"]["template"]["containers"][0]
    assert container["args"] == ["analytics"]
    assert container["image"] == "[parameters('agentImage')]"
    names = {value["name"] for value in container["env"]}
    assert names == {
        "CITADEL_PORTAL_COSMOS_ENDPOINT",
        "CITADEL_PORTAL_COSMOS_DATABASE",
        "CITADEL_PORTAL_ANALYTICS_CONTAINER",
        "CITADEL_PORTAL_MANAGED_IDENTITY_CLIENT_ID",
        "CITADEL_PORTAL_HUB_CONFIG",
    }
    assert "analyticsUsage" in compiled["resources"]["hosting"]["dependsOn"]
    usage_parameters = compiled["resources"]["analyticsUsage"]["properties"]["parameters"]
    assert "analyticsPrincipalId" in usage_parameters["agentPrincipalId"]["value"]
    analytics = next(
        item
        for item in all_resources
        if item["type"].endswith("/sqlDatabases/containers")
        and item["properties"]["resource"]["partitionKey"]["paths"] == ["/partitionKey"]
    )
    assert analytics["properties"]["resource"]["defaultTtl"] == -1
    templates = [
        item["properties"]["template"]
        for item in all_resources
        if item["type"] == "Microsoft.Resources/deployments"
        and "writerPrincipalId" in item["properties"]["template"].get("parameters", {})
    ]
    assert len(templates) == 1
    assignments = [item for item in resources(templates[0]) if item["type"].endswith("/sqlRoleAssignments")]
    writer = next(item for item in assignments if "writerPrincipalId" in item["properties"]["principalId"])
    reader = next(item for item in assignments if "readerPrincipalId" in item["properties"]["principalId"])
    assert "00000000-0000-0000-0000-000000000002" in writer["properties"]["roleDefinitionId"]
    assert "00000000-0000-0000-0000-000000000001" in reader["properties"]["roleDefinitionId"]
    assert "/colls/" in writer["properties"]["scope"] and "/colls/" in reader["properties"]["scope"]


@pytest.mark.az
def test_durable_assistant_job_has_no_ingress_or_retry_and_uses_the_agent_identity(compiled):
    all_resources = list(resources(compiled))
    job = next(
        item
        for item in all_resources
        if item["type"] == "Microsoft.App/jobs"
        and item["properties"]["template"]["containers"][0]["args"] == ["assistant-worker"]
    )
    config = job["properties"]["configuration"]
    assert config["triggerType"] == "Schedule" and config["replicaRetryLimit"] == 0
    assert config["scheduleTriggerConfig"]["cronExpression"] == "* * * * *"
    assert config["scheduleTriggerConfig"]["parallelism"] == 1
    assert config["replicaTimeout"] == 900
    assert "ingress" not in config and job["identity"]["type"] == "UserAssigned"
    env = job["properties"]["template"]["containers"][0]["env"]
    assert "CITADEL_PORTAL_DURABLE_TURNS_ENABLED" in json.dumps(env)
    agent = next(item for item in all_resources if item["type"] == "Microsoft.App/containerApps")
    assert job["identity"]["userAssignedIdentities"] == agent["identity"]["userAssignedIdentities"]


@pytest.mark.az
def test_agent_startup_depends_on_identity_and_external_roles_not_the_other_way_round(compiled):
    root = compiled["resources"]
    assert {"usage", "vault", "model", "gateway", "logs"}.issubset(root["hosting"]["dependsOn"])
    for role in ("usage", "vault", "model", "gateway", "logs"):
        assert "identities" in root[role]["dependsOn"]
        assert "hosting" not in root[role]["dependsOn"]
        assert "identities" in root[role]["properties"]["parameters"]["agentPrincipalId"]["value"]


@pytest.mark.az
def test_scanner_adapter_preserves_every_resource_body_and_condition(compiled):
    scanner = module("prepare_infra_scan")
    before = copy.deepcopy(compiled)
    adapted, count = scanner.scanner_template(compiled)
    assert compiled == before
    original = list(resources(compiled))
    transformed = list(resources(adapted))
    assert len(original) == len(transformed) == count
    for old, new in zip(original, transformed, strict=True):
        assert old["type"] == new["type"]
        assert old.get("condition") == new.get("condition")
        assert old.get("copy") == new.get("copy")
        if old["type"] != "Microsoft.Resources/deployments":
            assert old == new


def test_security_scan_cannot_succeed_with_zero_checks_or_parse_failures():
    scanner = module("prepare_infra_scan")
    for summary in (
        {"passed": 0, "failed": 0, "resource_count": 0},
        {"passed": 4, "failed": 1, "resource_count": 8},
        {"passed": 4, "parsing_errors": 1, "resource_count": 8},
    ):
        with pytest.raises(ValueError):
            scanner.verify_scan_result({"summary": summary})
    assert (
        scanner.verify_scan_result(
            {
                "summary": {
                    "passed": 15,
                    "failed": 0,
                    "resource_count": 40,
                    "parsing_errors": 0,
                }
            }
        )["passed"]
        == 15
    )
