# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Structural rejection of literal secrets in contract intents.

Contract intents are committed to a Git repository and rendered into ``.bicepparam`` files, so
they must never contain secret values. Secrets are referenced through Azure Key Vault instead
(``keyVaultSecretUri`` for backend credentials, Key Vault references for secure parameters).

The checks are deliberately strict:

* fields that only ever hold secrets (for example ``secretValue`` or ``awsSecretKey``) must not
  carry literal values
* redaction sentinels (``***redacted***`` from the gateway's live backend contract, or the agent's
  own ``[REDACTED]``) are rejected so redacted values can never be redeployed
* any string shaped like a secret (JWT, connection string, APIM key) is rejected
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from citadel_agent.safety.redaction import REDACTED, Redactor, default_redactor, normalize_key

UPSTREAM_REDACTION_SENTINEL = "***redacted***"
REDACTION_SENTINELS: tuple[str, ...] = (UPSTREAM_REDACTION_SENTINEL, REDACTED)

# Normalized names of fields that must never contain a literal value in an intent.
FORBIDDEN_LITERAL_FIELDS: frozenset[str] = frozenset(
    {
        "secretvalue",
        "awsaccesskey",
        "awssecretkey",
        "rotationkeyoverride",
        "apimsubscriptionkey",
        "explicitprimarykey",
        "explicitsecondarykey",
        "clientsecret",
        "entraclientsecret",
        "password",
        "apikey",
        "connectionstring",
        "accountkey",
        "sharedaccesskey",
    }
)


@dataclass(frozen=True)
class SecretFinding:
    """A location in an intent that holds (or looks like) a secret. Values are never included."""

    path: str
    reason: str


class SecretIngressError(ValueError):
    """Raised when an intent contains literal secrets or redaction sentinels."""

    def __init__(self, context: str, findings: list[SecretFinding]) -> None:
        details = "; ".join(f"{f.path}: {f.reason}" for f in findings)
        super().__init__(f"{context} contains values that must not be stored: {details}")
        self.findings = findings


def _is_secret_shaped(text: str, redactor: Redactor) -> bool:
    return redactor.redact(text) != text


def find_secret_literals(
    value: Any, path: str = "$", *, redactor: Redactor | None = None
) -> list[SecretFinding]:
    """Return every location in ``value`` that holds a literal secret or a redaction sentinel.

    Args:
        value: Intent data (nested mappings, lists and scalars).
        path: JSONPath-style location of ``value`` (used in findings).
        redactor: Redactor used to recognize secret-shaped strings.

    Returns:
        Findings in traversal order; empty when the data is clean.
    """
    scanner = redactor or default_redactor()
    findings: list[SecretFinding] = []

    def visit(node: Any, location: str, field_name: str | None) -> None:
        if isinstance(node, Mapping):
            for key, item in node.items():
                visit(item, f"{location}.{key}", str(key))
            return
        if isinstance(node, list | tuple):
            for index, item in enumerate(node):
                visit(item, f"{location}[{index}]", field_name)
            return
        if not isinstance(node, str) or not node:
            return
        if any(sentinel in node for sentinel in REDACTION_SENTINELS):
            findings.append(
                SecretFinding(location, "redacted placeholder; supply a Key Vault reference instead")
            )
        elif field_name is not None and normalize_key(field_name) in FORBIDDEN_LITERAL_FIELDS:
            findings.append(SecretFinding(location, "literal secret; use a Key Vault reference instead"))
        elif _is_secret_shaped(node, scanner):
            findings.append(
                SecretFinding(location, "value looks like a secret (key, token or connection string)")
            )

    visit(value, path, None)
    return findings


def ensure_no_secret_literals(value: Any, *, context: str, redactor: Redactor | None = None) -> None:
    """Raise :class:`SecretIngressError` when ``value`` contains literal secrets or sentinels."""
    findings = find_secret_literals(value, redactor=redactor)
    if findings:
        raise SecretIngressError(context, findings)
