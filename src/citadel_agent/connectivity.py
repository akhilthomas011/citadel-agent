# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tell network problems from missing permissions when an Azure data-plane call fails.

Citadel's Key Vault, Cosmos DB and Foundry accounts are private by default, so a developer
machine usually fails on the network before permissions matter. The two need different fixes,
and the error text alone is easy to misread (a firewall block is also an HTTP 403).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["firewall", "rbac", "unreachable", "unknown"]

_UNREACHABLE = re.compile(
    r"name or service not known|getaddrinfo|failed to resolve|nameresolution|nodename nor servname|"
    r"connect ?timeout|timed out|connection refused|private ?link|no route to host",
    re.IGNORECASE,
)
_FIREWALL = re.compile(
    r"firewall|forbiddenbyfirewall|client address is not authorized|originated from ip|"
    r"public network access is disabled|ipforbidden|not allowed (?:by|through) (?:the )?network",
    re.IGNORECASE,
)
_RBAC = re.compile(
    r"does not have (?:the )?(?:required )?(?:rbac )?permission|authorizationpermissionmismatch|"
    r"authorizationfailed|caller is not authorized to perform action|not authorized to perform action|"
    r"forbidden|\b403\b|unauthorized|\b401\b",
    re.IGNORECASE,
)
_CLIENT_IP = re.compile(
    r"(?:originated from ip|client address:?|client ip(?: address)?:?|from ip)\s*(\d{1,3}(?:\.\d{1,3}){3})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Diagnosis:
    kind: Kind
    summary: str
    client_ip: str | None = None


def client_ip(text: str) -> str | None:
    """The caller's public IP address when the service reports it."""
    match = _CLIENT_IP.search(text)
    return match.group(1) if match else None


def diagnose(error: str, resource: str = "the resource", needed_roles: str | None = None) -> Diagnosis:
    """Classify a failure and say what to do about it.

    Order matters: unreachable hosts and firewall blocks are checked before permissions because a
    firewall block is reported as an HTTP 403 as well.
    """
    ip = client_ip(error)
    where = f" from {ip}" if ip else ""
    if _UNREACHABLE.search(error) and not _FIREWALL.search(error):
        return Diagnosis(
            "unreachable",
            f"{resource} could not be reached. It may only be reachable inside the hub's virtual network: "
            "connect through the platform team's VPN or jump box, or run the agent in that network.",
        )
    if _FIREWALL.search(error):
        return Diagnosis(
            "firewall",
            f"Network rules on {resource} block this machine{where}. Ask the platform team to allow "
            "your address (development environments only), or connect through the VPN or jump box. "
            "This is not a permissions problem.",
            ip,
        )
    if _RBAC.search(error):
        roles = f" Needs {needed_roles}." if needed_roles else ""
        return Diagnosis(
            "rbac",
            f"You can reach {resource} but your account is not allowed to use it.{roles} "
            "Ask the platform team to grant the role.",
        )
    first = error.strip().splitlines()[0][:180] if error.strip() else "unknown error"
    return Diagnosis("unknown", first)
