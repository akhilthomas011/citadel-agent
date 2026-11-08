# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Shared deployment to verified private hosts, used by updates and the customer installer."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from citadel_agent.proc import run_command

_IMAGE = re.compile(r"^[a-z0-9][a-z0-9./_-]+@sha256:[0-9a-f]{64}$")


def validate_hosts(
    web: dict[str, Any], agent: dict[str, Any], environment: dict[str, Any], image: str
) -> None:
    if not web.get("reserved") or "linux" not in str(web.get("kind", "")).lower():
        raise ValueError(
            "A dedicated existing Linux App Service is required. Windows apps are not converted."
        )
    if not _IMAGE.fullmatch(image):
        raise ValueError("The agent image must be pinned by a SHA-256 digest, not a mutable tag.")
    subnet = web.get("virtualNetworkSubnetId")
    if not subnet:
        raise ValueError("App Service regional VNet integration must be configured before deployment.")
    properties = environment.get("properties", {})
    network = properties.get("vnetConfiguration") or {}
    if network.get("internal") is not True:
        raise ValueError("The agent environment must be INTERNAL with no public load balancer.")
    if not network.get("infrastructureSubnetId"):
        raise ValueError("The Container Apps environment must be integrated with a dedicated subnet.")
    ingress = agent.get("properties", {}).get("configuration", {}).get("ingress") or {}
    if ingress.get("external") is not True or ingress.get("allowInsecure") is not False:
        raise ValueError("The app needs HTTPS VNet-reachable ingress inside the INTERNAL environment.")
    if ingress.get("targetPort") != 8000:
        raise ValueError("The prepared agent target port must be 8000.")
    template = agent.get("properties", {}).get("template", {})
    if len(template.get("containers", [])) != 1:
        raise ValueError("The deployment workflow supports a dedicated, single-container agent app.")
    probes = template["containers"][0].get("probes", [])
    if not any(probe.get("type") == "Readiness" for probe in probes):
        raise ValueError("Configure a Container Apps readiness probe before deploying the agent.")


def az_json(arguments: list[str]) -> Any:
    result = run_command("az", [*arguments, "--output", "json"], timeout=900)
    return json.loads(result.stdout) if result.stdout.strip() else None


def deploy(
    args: argparse.Namespace,
    *,
    command: Callable[[list[str]], Any] = az_json,
    health: Callable[[str], None] | None = None,
) -> None:
    common = ["--subscription", args.subscription]
    web_args = ["--resource-group", args.web_resource_group, "--name", args.web_app, *common]
    agent_args = ["--resource-group", args.agent_resource_group, "--name", args.agent_app, *common]
    web = command(["webapp", "show", *web_args])
    agent = command(["containerapp", "show", *agent_args])
    environment_id = agent.get("properties", {}).get("environmentId") or agent.get("properties", {}).get(
        "managedEnvironmentId"
    )
    if not isinstance(environment_id, str) or not environment_id:
        raise ValueError("The agent's environment ID is missing.")
    environment = command(["resource", "show", "--ids", environment_id, *common])
    validate_hosts(web, agent, environment, args.agent_image)
    settings = command(["webapp", "config", "appsettings", "list", *web_args])
    configured = {item["name"]: item.get("value", "") for item in settings}
    if not configured.get("CITADEL_PORTAL_AGENT_API_URL") or not configured.get(
        "CITADEL_PORTAL_MANAGED_IDENTITY_CLIENT_ID"
    ):
        raise ValueError("Configure the BFF's portal settings and managed identity before deploying.")
    expected_url = "https://" + agent["properties"]["configuration"]["ingress"]["fqdn"]
    if configured["CITADEL_PORTAL_AGENT_API_URL"].rstrip("/") != expected_url:
        raise ValueError("The BFF is configured for a different private agent endpoint.")
    agent_variables = {
        item["name"]: item for item in agent["properties"]["template"]["containers"][0].get("env", [])
    }
    required = (
        "CITADEL_PORTAL_HUB_CONFIG",
        "CITADEL_PORTAL_BFF_SERVICE_CLIENT_ID",
        "CITADEL_PORTAL_MANAGED_IDENTITY_CLIENT_ID",
    )
    if any(not agent_variables.get(name) for name in required):
        raise ValueError("Configure the private agent's hub configuration and service identity first.")
    if not args.bundle.is_file():
        raise ValueError("The built App Service ZIP was not found.")
    origin = configured.get("CITADEL_PORTAL_PORTAL_ORIGIN", "")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("A canonical HTTPS portal origin is required for the post-deployment health check.")
    print("Preflight passed: dedicated Linux BFF, internal ACA, HTTPS ingress and matching runtime targets.")
    if not args.apply:
        print("Validation only. No Azure resources or application revisions were changed.")
        return
    command(["containerapp", "update", *agent_args, "--image", args.agent_image])
    command(
        [
            "webapp",
            "config",
            "appsettings",
            "set",
            *web_args,
            "--settings",
            "SCM_DO_BUILD_DURING_DEPLOYMENT=false",
            "ENABLE_ORYX_BUILD=false",
        ]
    )
    command(
        [
            "webapp",
            "config",
            "set",
            *web_args,
            "--startup-file",
            "PYTHONPATH=/home/site/wwwroot/.python_packages/lib/site-packages "
            "python /home/site/wwwroot/app.py",
            "--linux-fx-version",
            "PYTHON|3.11",
            "--always-on",
            "true",
            "--http20-enabled",
            "true",
        ]
    )
    command(
        [
            "webapp",
            "deploy",
            *web_args,
            "--src-path",
            str(args.bundle.resolve()),
            "--type",
            "zip",
            "--clean",
            "true",
            "--restart",
            "true",
        ]
    )
    (health or check_health)(origin)
    print(
        "Deployment completed and BFF state-store readiness passed. "
        "Run the authenticated acceptance checklist before declaring product readiness."
    )


def check_health(origin: str) -> None:
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        for _ in range(24):
            try:
                response = client.get(origin.rstrip("/") + "/health/ready")
                if response.status_code == 200 and response.json().get("status") == "ready":
                    session = client.get(origin.rstrip("/") + "/auth/session")
                    if session.status_code == 200 and session.json() == {"authenticated": False}:
                        return
            except (httpx.RequestError, ValueError):
                print("Waiting for the deployed BFF to become responsive.")
            time.sleep(5)
    raise RuntimeError(
        "Post-deployment BFF readiness did not pass. Inspect the hosts; do not expose the agent."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "subscription",
        "web-resource-group",
        "web-app",
        "agent-resource-group",
        "agent-app",
        "agent-image",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Deploy to the prepared hosts after preflight.")
    deploy(parser.parse_args())
    return 0


if __name__ == "__main__":
    sys.exit(main())
