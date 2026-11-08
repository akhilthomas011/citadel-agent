"""Customer installation previews, scoped infrastructure and consent boundaries without live Azure."""

import copy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from citadel_agent.portal.installation import (
    HostingInstallConfig,
    HostingNetworkConfig,
    install,
    main,
    preflight,
    template_parameters,
    verify_bundle,
    verify_preview,
)

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/example/citadel@sha256:" + "a" * 64


@pytest.fixture
def config():
    return HostingInstallConfig.model_validate_json((ROOT / "infra" / "install.example.json").read_text())


class Azure:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.role_assigned = True
        self.preview = {"status": "Succeeded", "changes": []}
        self.windows = False
        self.integrated_subnet = None
        self.vnets = []
        self.existing_resources = []
        self.existing_group = False
        self.approved_connections = True
        self.writes = []
        self.properties = {
            config.bff_identity.name: {"clientId": str(uuid4()), "principalId": str(uuid4())},
            config.apim.name: {"gatewayUrl": config.hub.gateway_url, "publicNetworkAccess": "Enabled"},
            config.key_vault.name: {"enableRbacAuthorization": True, "enablePurgeProtection": True},
            config.usage_account.name: {"documentEndpoint": config.hub.usage_endpoint},
            config.model_account.name: {"customSubDomainName": config.model_account.name},
            config.logs.name: {"customerId": config.hub.log_analytics_workspace_id},
        }
        self.role_id = str(uuid4())
        self.service_id = str(uuid4())

    def __call__(self, args):
        self.calls.append(args)
        c = self.config
        if args[:2] == ["account", "show"]:
            return {"id": c.subscription_id, "tenantId": c.hub.scope.tenant_id}
        if args[:2] == ["account", "list-locations"]:
            return [{"name": "swedencentral", "availabilityZoneMappings": [{"logicalZone": "1"}]}]
        if args[:2] == ["webapp", "show"]:
            return {
                "reserved": not self.windows,
                "kind": "app,linux" if not self.windows else "app",
                "serverFarmId": "/subscriptions/test/providers/Microsoft.Web/serverfarms/test",
                "location": "Sweden Central",
                "defaultHostName": c.web_app.name + ".azurewebsites.net",
                "hostNames": [c.web_app.name + ".azurewebsites.net"],
                "virtualNetworkSubnetId": self.integrated_subnet,
            }
        if args[:3] == ["appservice", "plan", "show"]:
            return {"sku": {"tier": "Standard"}}
        if args[:2] == ["resource", "show"]:
            resource_id = args[args.index("--ids") + 1]
            name = resource_id.rsplit("/", 1)[-1]
            return {
                "id": resource_id,
                "location": "swedencentral",
                "properties": self.properties.get(name, {}),
            }
        if args[:3] == ["network", "vnet", "list"]:
            return self.vnets
        if args[:3] == ["network", "private-endpoint", "list"]:
            return [
                {
                    "name": f"pe-{c.name_prefix}-{name}",
                    "privateLinkServiceConnections": [
                        {
                            "privateLinkServiceConnectionState": {
                                "status": "Approved" if self.approved_connections else "Pending",
                            }
                        }
                    ],
                }
                for name in ("state", "usage", "vault", "model", "gateway")
            ]
        if args[:2] == ["group", "exists"]:
            return self.existing_group
        if args[:2] == ["group", "show"]:
            return {"tags": {"customer": "preserved"}}
        if args[:2] == ["resource", "list"]:
            return self.existing_resources
        if args[:3] == ["ad", "app", "show"]:
            return {
                "appId": args[args.index("--id") + 1],
                "signInAudience": "AzureADMyOrg",
                "identifierUris": ["api://" + c.entra.api_client_id],
                "api": {
                    "requestedAccessTokenVersion": 2,
                    "oauth2PermissionScopes": [{"value": c.entra.delegated_scope_name, "isEnabled": True}],
                },
                "web": {"redirectUris": [f"https://{c.web_app.name}.azurewebsites.net/auth/callback"]},
            }
        if args[:3] == ["ad", "sp", "show"]:
            return {
                "id": self.service_id,
                "appRoles": [
                    {
                        "id": self.role_id,
                        "value": "Portal.InvokeAgent",
                        "isEnabled": True,
                        "allowedMemberTypes": ["Application"],
                    },
                    *[
                        {"value": name, "isEnabled": True, "allowedMemberTypes": ["User"]}
                        for name in (
                            "Platform.Viewer",
                            "Platform.Operator",
                            "Platform.Approver",
                            "Platform.FinOpsAnalyst",
                        )
                    ],
                ],
            }
        if args[:4] == ["rest", "--method", "get", "--url"]:
            uri = args[4]
            if "privateLinkResources" in uri:
                return {"value": [{"properties": {"groupId": "Gateway"}}]}
            return {
                "value": [{"resourceId": self.service_id, "appRoleId": self.role_id}]
                if self.role_assigned
                else []
            }
        if args[:3] == ["deployment", "sub", "what-if"]:
            parameters = Path(args[args.index("--parameters") + 1].removeprefix("@"))
            payload = json.loads(parameters.read_text())
            assert payload["parameters"]["agentImage"]["value"] == IMAGE
            return self.preview
        if args[:3] == ["deployment", "sub", "validate"]:
            return {"properties": {"provisioningState": "Succeeded"}}
        if args[:3] == ["deployment", "sub", "create"]:
            self.writes.append(args)
            return {
                "properties": {
                    "provisioningState": "Succeeded",
                    "outputs": {"hosts": {"value": self.hosts()}},
                }
            }
        if args[0] == "webapp" and any(action in args for action in ("assign", "add", "set")):
            self.writes.append(args)
            if "--settings" in args:
                self.web_settings = json.loads(Path(args[args.index("--settings") + 1][1:]).read_text())
            return {}
        if args[:3] == ["rest", "--method", "patch"]:
            self.writes.append(args)
            return {}
        pytest.fail(f"Unrecognized installer command: {args}")

    def hosts(self):
        c = self.config
        return {
            "agentApp": f"ca-{c.name_prefix}-agent",
            "agentResourceGroup": c.resource_group_name,
            "agentApiUrl": "https://agent.private.example",
            "bffIdentityId": c.resource_id(
                c.bff_identity, "Microsoft.ManagedIdentity/userAssignedIdentities"
            ),
            "bffIdentityClientId": self.properties[c.bff_identity.name]["clientId"],
            "bffSubnetId": (
                f"/subscriptions/{c.subscription_id}/resourceGroups/{c.resource_group_name}"
                f"/providers/Microsoft.Network/virtualNetworks/vnet-{c.name_prefix}/subnets/bff"
            ),
            "stateEndpoint": "https://state.documents.azure.com",
            "stateDatabase": "citadel-portal",
            "stateContainer": "portal-state",
        }


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "citadel-portal.zip"
    path.write_bytes(b"test-publication-artifact")
    path.with_name("hosted-manifest.json").write_text(
        json.dumps(
            {
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "imagePublished": True,
                "agentImage": IMAGE,
            }
        )
    )
    return path


def test_preview_only_is_end_to_end_and_never_mutates_azure(config, bundle):
    azure = Azure(config)
    result = install(
        config, template=ROOT / "infra" / "main.bicep", image=IMAGE, bundle=bundle, command=azure
    )
    assert result["status"] == "previewed"
    assert result["location"] == "swedencentral"
    assert not azure.writes
    assert not any("create" in args or "set" in args or "patch" in args for args in azure.calls)
    assert any("appRoleAssignments" in str(args) for args in azure.calls)
    assert any("FullResourcePayloads" in args for args in azure.calls)
    assert any(args[:3] == ["deployment", "sub", "validate"] for args in azure.calls)


def test_apply_provisions_then_configures_only_selected_host_and_deploys(config, bundle):
    azure = Azure(config)
    deliveries = []
    result = install(
        config,
        template=ROOT / "infra" / "main.bicep",
        image=IMAGE,
        bundle=bundle,
        apply=True,
        command=azure,
        deploy_application=lambda *args: deliveries.append(args),
    )
    assert result["status"] == "installed"
    assert len(deliveries) == 1
    assert azure.writes[0][:3] == ["deployment", "sub", "create"]
    assert "--mode" not in azure.writes[0] or "Complete" not in azure.writes[0]
    assert azure.web_settings["CITADEL_PORTAL_AGENT_API_AUDIENCE"] == config.entra.api_client_id
    assert (
        azure.web_settings["CITADEL_PORTAL_AGENT_API_TOKEN_SCOPE"]
        == f"api://{config.entra.api_client_id}/.default"
    )
    assert "CITADEL_PORTAL_HUB_CONFIG" not in azure.web_settings
    assert azure.web_settings["CITADEL_PORTAL_BFF_SERVICE_CLIENT_ID"] == azure.hosts()["bffIdentityClientId"]
    assert all("secretValue" not in str(args) for args in azure.calls)


@pytest.mark.parametrize(
    "condition", ["windows", "missing-role", "wrong-vnet", "network-overlap", "unrelated-group"]
)
def test_unsafe_targets_are_rejected_before_provisioning(config, bundle, condition):
    azure = Azure(config)
    if condition == "windows":
        azure.windows = True
    elif condition == "missing-role":
        azure.role_assigned = False
    elif condition == "wrong-vnet":
        azure.integrated_subnet = "/another/network/subnet"
    elif condition == "network-overlap":
        azure.vnets = [{"id": "/another/vnet", "addressSpace": {"addressPrefixes": ["10.244.0.0/16"]}}]
    else:
        azure.existing_group = True
        azure.existing_resources = [
            {"id": "/unrelated/resource", "type": "Microsoft.Storage/storageAccounts", "tags": {}}
        ]
    with pytest.raises(ValueError):
        install(
            config,
            template=ROOT / "infra" / "main.bicep",
            image=IMAGE,
            bundle=bundle,
            apply=True,
            command=azure,
            deploy_application=lambda *args: pytest.fail("must not deploy"),
        )
    assert not azure.writes


@pytest.mark.parametrize("change_type", ["Delete", "Unsupported", "Deploy", "Modify"])
def test_preview_blocks_destructive_incomplete_or_existing_gateway_changes(config, bundle, change_type):
    azure = Azure(config)
    azure.preview["changes"] = [{"changeType": change_type, "resourceId": config.hub.apim_resource_id}]
    with pytest.raises(ValueError):
        install(
            config,
            template=ROOT / "infra" / "main.bicep",
            image=IMAGE,
            bundle=bundle,
            apply=True,
            command=azure,
            deploy_application=lambda *args: None,
        )
    assert not azure.writes


def test_scoped_role_and_new_host_changes_are_permitted_but_not_other_subscription_writes(config):
    group = f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group_name}"
    usage = config.resource_id(config.usage_account, "Microsoft.DocumentDB/databaseAccounts")
    preview = {
        "status": "Succeeded",
        "changes": [
            {
                "changeType": "Create",
                "resourceId": group + "/providers/Microsoft.Network/virtualNetworks/vnet-citadel",
            },
            {"changeType": "Create", "resourceId": usage + "/sqlRoleAssignments/" + str(uuid4())},
        ],
    }
    verify_preview(config, preview)
    preview["changes"].append(
        {
            "changeType": "Create",
            "resourceId": "/subscriptions/other/providers/Microsoft.Authorization/roleAssignments/bad",
        }
    )
    with pytest.raises(ValueError):
        verify_preview(config, preview)


def test_template_values_match_runtime_and_preserve_resource_group_tags(config):
    azure = Azure(config)
    azure.existing_group = True
    values = template_parameters(config, preflight(config, azure), IMAGE)["parameters"]
    assert values["tags"]["value"]["customer"] == "preserved"
    settings = values["agentSettings"]["value"]
    assert settings["CITADEL_PORTAL_API_AUDIENCE"] == config.entra.api_client_id
    assert settings["CITADEL_PORTAL_AGENT_API_TOKEN_SCOPE"].endswith("/.default")
    assert type(config.hub).model_validate_json(settings["CITADEL_PORTAL_HUB_CONFIG"]) == config.hub
    grants = json.loads(settings["CITADEL_PORTAL_HUB_CONFIG"])["initialGrants"]
    assert all(grant["permissions"] == sorted(grant["permissions"]) for grant in grants)
    assert all("Value" not in name for name in settings)


def test_bad_configuration_and_unreviewed_networks_are_rejected(config):
    for fields in (
        {"bffPrefix": "10.244.0.0/26"},
        {"agentPrefix": "10.244.0.0/27"},
        {"addressPrefix": "8.0.0.0/8"},
        {
            "addressPrefix": "169.254.0.0/16",
            "agentPrefix": "169.254.0.0/23",
            "bffPrefix": "169.254.2.0/26",
            "endpointPrefix": "169.254.2.64/27",
        },
    ):
        with pytest.raises(ValidationError):
            HostingNetworkConfig(**fields)
    raw = config.model_dump(by_alias=True, mode="json")
    for key, value in (("namePrefix", "invalid--prefix"), ("clientSecret", "not-allowed")):
        invalid = copy.deepcopy(raw)
        invalid[key] = value
        with pytest.raises(ValidationError):
            HostingInstallConfig.model_validate(invalid)
    invalid = copy.deepcopy(raw)
    invalid["modelAccount"]["subscriptionId"] = str(uuid4())
    with pytest.raises(ValidationError, match="target subscription"):
        HostingInstallConfig.model_validate(invalid)
    invalid = copy.deepcopy(raw)
    invalid["bffIdentity"]["resourceGroupName"] = config.resource_group_name
    invalid["bffIdentity"]["name"] = f"id-{config.name_prefix}-agent"
    with pytest.raises(ValidationError, match="distinct"):
        HostingInstallConfig.model_validate(invalid)


def test_missing_zone_support_requires_an_explicit_configuration_decision(config, bundle):
    azure = Azure(config)

    def command(args):
        if args[:2] == ["account", "list-locations"]:
            return [{"name": "swedencentral", "availabilityZoneMappings": []}]
        return azure(args)

    with pytest.raises(ValueError, match="does not advertise availability zones"):
        install(config, template=ROOT / "infra" / "main.bicep", image=IMAGE, bundle=bundle, command=command)
    assert not azure.writes


def test_tampered_bundle_and_missing_application_deployer_stop_before_cloud_reads(config, bundle):
    bundle.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="manifest"):
        install(
            config,
            template=ROOT / "infra" / "main.bicep",
            image=IMAGE,
            bundle=bundle,
            command=lambda _: pytest.fail("no Azure reads for bad artifact"),
        )


def test_image_digest_must_come_from_the_same_published_artifact_run(bundle):
    verify_bundle(bundle, expected_image=IMAGE)
    other = "ghcr.io/example/citadel@sha256:" + "b" * 64
    with pytest.raises(ValueError, match="published digest"):
        verify_bundle(bundle, expected_image=other)
    manifest_path = bundle.with_name("hosted-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["imagePublished"] = False
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="published digest"):
        verify_bundle(bundle, expected_image=IMAGE)


def test_unapproved_private_connections_stop_before_bff_updates(config, bundle):
    azure = Azure(config)
    azure.approved_connections = False
    with pytest.raises(RuntimeError, match="not approved"):
        install(
            config,
            template=ROOT / "infra" / "main.bicep",
            image=IMAGE,
            bundle=bundle,
            apply=True,
            command=azure,
            deploy_application=lambda *args: pytest.fail("must not deploy"),
        )
    assert len(azure.writes) == 1
    assert azure.writes[0][:3] == ["deployment", "sub", "create"]


def test_invalid_config_never_echoes_a_pasted_secret_to_logs(config, bundle, tmp_path, caplog):
    raw = config.model_dump(mode="json", by_alias=True)
    raw["clientSecret"] = "private-literal-never-print"
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw))
    result = main(["--config", str(path), "--bundle", str(bundle), "--image", IMAGE])
    assert result == 2
    assert "private-literal-never-print" not in caplog.text
    assert "Values are not logged" in caplog.text


def test_install_workflow_requires_provenance_protected_environment_and_explicit_apply():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "install-hosted.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["apply"]["default"] is False
    assert workflow["jobs"]["verify"]["uses"] == "./.github/workflows/verify-hosted-artifact.yml"
    job = workflow["jobs"]["install"]
    assert job["needs"] == "verify"
    assert job["environment"] == "${{ inputs.environment }}"
    assert job["permissions"]["id-token"] == "write"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "--expected-subscription" in str(job)
