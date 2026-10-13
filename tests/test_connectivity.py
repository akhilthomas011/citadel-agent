# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Network and permission failures must be told apart, because they need different fixes."""

from __future__ import annotations

from pathlib import Path

import pytest

from citadel_agent.azure_clients.keyvault import KeyVaultAccessError
from citadel_agent.connectivity import client_ip, diagnose
from citadel_agent.tools import operations
from tool_fakes import FakeContext, make_intent, make_record

COSMOS_FIREWALL = (
    'Response status code does not indicate success: Forbidden (403); Reason: ({"code":"Forbidden",'
    '"message":"Request originated from IP 203.0.113.7 through public internet. This is blocked by your '
    'Cosmos DB account firewall settings. More info: https://aka.ms/cosmosdb-tsg-forbidden"}); '
    "SDK: Windows/10.0.26200 cosmos-netstandard-sdk/3.18.0"
)
COSMOS_RBAC = (
    "(Forbidden) Request blocked by Auth cosmos-1 : Request is blocked because principal [abc] does not "
    "have required RBAC permissions to perform action [Microsoft.DocumentDB/databaseAccounts/readMetadata]"
)
LOG_RBAC = (
    "(InsufficientAccessError) The provided credentials have insufficient access to perform the operation"
)
DNS = "HTTPSConnectionPool: Failed to resolve 'cosmos-1.documents.azure.com' (getaddrinfo failed)"


def test_a_firewall_block_is_not_mistaken_for_missing_permissions() -> None:
    result = diagnose(COSMOS_FIREWALL, "Cosmos DB cosmos-1", "Cosmos DB Built-in Data Reader")

    assert result.kind == "firewall"
    assert result.client_ip == "203.0.113.7"
    assert "203.0.113.7" in result.summary and "not a permissions problem" in result.summary
    assert "Built-in Data Reader" not in result.summary


def test_missing_permissions_name_the_role_to_request() -> None:
    result = diagnose(COSMOS_RBAC, "Cosmos DB cosmos-1", "Cosmos DB Built-in Data Reader")

    assert result.kind == "rbac"
    assert "Cosmos DB Built-in Data Reader" in result.summary
    assert diagnose(LOG_RBAC, "Log Analytics").kind in {"rbac", "unknown"}


def test_unresolvable_hosts_point_at_private_networking() -> None:
    result = diagnose(DNS, "Cosmos DB cosmos-1")

    assert result.kind == "unreachable" and "virtual network" in result.summary


def test_unknown_errors_keep_only_the_first_line() -> None:
    result = diagnose("Something odd\nsecond line with detail", "x")

    assert result.kind == "unknown" and result.summary == "Something odd"
    assert diagnose("", "x").kind == "unknown"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Request originated from IP 198.51.100.4 through public internet", "198.51.100.4"),
        ("Client address: 198.51.100.9\nVault: kv", "198.51.100.9"),
        ("SDK: Windows/10.0.20348.0 cosmos-netstandard-sdk/3.18.0", None),
    ],
)
def test_client_ip_is_taken_only_from_the_services_own_wording(text: str, expected: str | None) -> None:
    assert client_ip(text) == expected


def test_preflight_check_status_follows_the_diagnosis() -> None:
    def fail(message: str):
        def run() -> str:
            raise RuntimeError(message)

        return run

    assert operations._check("x", fail(COSMOS_FIREWALL), "Cosmos DB")[0] == "blocked"
    assert operations._check("x", fail(COSMOS_RBAC), "Cosmos DB", "a role") == (
        "missing role",
        diagnose(COSMOS_RBAC, "Cosmos DB", "a role").summary,
    )
    assert operations._check("x", fail(DNS), "Cosmos DB")[0] == "unreachable"
    assert operations._check("x", lambda: "fine") == ("ok", "fine")


def test_key_vault_denials_keep_their_own_guidance() -> None:
    def blocked() -> str:
        raise KeyVaultAccessError("Key Vault blocked the request from 1.2.3.4 (network rules).")

    def denied() -> str:
        raise KeyVaultAccessError("You are not allowed to read secrets in the vault.")

    assert operations._check("kv", blocked)[0] == "blocked"
    assert operations._check("kv", denied)[0] == "missing role"


def test_usage_report_explains_why_cosmos_was_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = FakeContext(tmp_path, [make_record(make_intent())])

    def broken(*args: object) -> None:
        raise RuntimeError(COSMOS_FIREWALL)

    monkeypatch.setattr(operations, "_cosmos_usage", broken)
    monkeypatch.setattr(operations, "_metric_usage", lambda *args: [])

    text = operations.usage_report(context)

    assert "203.0.113.7" in text and "not a permissions problem" in text
