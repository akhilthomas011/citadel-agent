# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Redaction of secrets from text and structured data.

Redaction is a defense-in-depth layer: the agent's tools are designed to never return secret
values, and contract intents reject literal secrets structurally (see
:mod:`citadel_agent.safety.secret_ingress`). The redactor still scrubs every tool result, log line
and audit record in case a secret slips through.

Two mechanisms are combined:

* exact replacement of secrets the agent handled at runtime (registered with
  :meth:`Redactor.register_secret`), for example a subscription key read for a smoke test
* pattern and key-name based replacement for well-known secret shapes (APIM keys, JWTs,
  connection strings, SAS signatures, bearer tokens, AWS access key IDs)
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Normalized (lower-case, without '-' or '_') names of fields whose values are always secret.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "apikey",
        "subscriptionkey",
        "ocpapimsubscriptionkey",
        "apimsubscriptionkey",
        "primarykey",
        "secondarykey",
        "explicitprimarykey",
        "explicitsecondarykey",
        "rotationkeyoverride",
        "secretvalue",
        "clientsecret",
        "entraclientsecret",
        "password",
        "accountkey",
        "sharedaccesskey",
        "connectionstring",
        "authorization",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "awssecretkey",
        "awsaccesskey",
        "awssecretaccesskey",
        "xapikey",
        "xgoogapikey",
    }
)

# (pattern, replacement) pairs applied in order. Replacements may keep a readable prefix.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Secret-bearing HTTP headers written as "Name: value" lines.
    (
        re.compile(
            r"(?im)^(\s*(?:api-key|ocp-apim-subscription-key|authorization|x-api-key|x-goog-api-key)\s*:\s*).+$"
        ),
        r"\g<1>" + REDACTED,
    ),
    # JSON Web Tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), REDACTED),
    # Bearer tokens.
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\g<1>" + REDACTED),
    # Connection-string style secrets.
    (
        re.compile(
            r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature|ClientSecret|Password)=[^;\s\"']+"
        ),
        r"\g<1>=" + REDACTED,
    ),
    # SAS signatures in URLs.
    (re.compile(r"(?i)([?&]sig=)[^&\s\"']+"), r"\g<1>" + REDACTED),
    # AWS access key IDs.
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    # APIM rotation keys (64 hex) and subscription keys (32 hex). Hex preceded by ':' is not
    # matched so agent content hashes ("sha256:<hex>") stay readable.
    (re.compile(r"(?<![0-9A-Fa-f:])[0-9a-f]{64}(?![0-9A-Fa-f])"), REDACTED),
    (re.compile(r"(?<![0-9A-Fa-f:])[0-9a-f]{32}(?![0-9A-Fa-f])"), REDACTED),
)

_MIN_REGISTERED_SECRET_LENGTH = 8


def normalize_key(name: str) -> str:
    """Normalize a field name for sensitive-key matching."""
    return name.replace("-", "").replace("_", "").lower()


def is_sensitive_key(name: str) -> bool:
    """Return True when a field name always holds a secret value."""
    return normalize_key(name) in SENSITIVE_KEYS


class Redactor:
    """Scrubs secrets from text and nested data structures. Thread-safe."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register_secret(self, value: str | None) -> None:
        """Register a secret value handled at runtime so it is replaced wherever it appears."""
        if value and len(value) >= _MIN_REGISTERED_SECRET_LENGTH:
            with self._lock:
                self._secrets.add(value)

    def register_secrets(self, values: Iterable[str | None]) -> None:
        """Register several runtime secrets."""
        for value in values:
            self.register_secret(value)

    def redact(self, text: str) -> str:
        """Return ``text`` with registered secrets and known secret shapes replaced."""
        if not text:
            return text
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, REDACTED)
        for pattern, replacement in _RULES:
            text = pattern.sub(replacement, text)
        return text

    def redact_obj(self, value: Any) -> Any:
        """Return a copy of ``value`` with secret fields and secret-shaped strings redacted."""
        if isinstance(value, Mapping):
            redacted: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and is_sensitive_key(key) and item not in (None, ""):
                    redacted[key] = REDACTED
                else:
                    redacted[key] = self.redact_obj(item)
            return redacted
        if isinstance(value, list | tuple):
            return [self.redact_obj(item) for item in value]
        if isinstance(value, str):
            return self.redact(value)
        return value


_default_redactor = Redactor()


def default_redactor() -> Redactor:
    """Return the process-wide redactor shared by tools, logging and auditing."""
    return _default_redactor
