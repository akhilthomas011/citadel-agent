# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for secret-ingress detection and the audit log."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from citadel_agent.safety import (
    REDACTED,
    AuditLog,
    AuditOutcome,
    Redactor,
    SecretIngressError,
    ensure_no_secret_literals,
    find_secret_literals,
)


def test_clean_intent_has_no_findings() -> None:
    intent = {
        "backend": {
            "authType": "api-key-bearer",
            "authConfig": {
                "namedValueKey": "gemini-key",
                "keyVaultSecretUri": "https://kv-dev.vault.azure.net/secrets/gemini-key",
            },
        },
        "models": ["gpt-5.2", "text-embedding-3-large"],
    }
    assert find_secret_literals(intent, redactor=Redactor()) == []


def test_forbidden_literal_fields_are_reported_with_paths() -> None:
    intent = {"llmBackendConfig": [{"authConfig": {"secretValue": "plain"}}], "awsSecretKey": "x"}
    findings = find_secret_literals(intent, redactor=Redactor())
    paths = {finding.path for finding in findings}
    assert "$.llmBackendConfig[0].authConfig.secretValue" in paths
    assert "$.awsSecretKey" in paths


@pytest.mark.parametrize("sentinel", ["***redacted***", REDACTED])
def test_redaction_sentinels_are_rejected(sentinel: str) -> None:
    findings = find_secret_literals({"authConfig": {"namedValueKey": sentinel}}, redactor=Redactor())
    assert findings and "redacted" in findings[0].reason


def test_secret_shaped_values_are_rejected_anywhere() -> None:
    findings = find_secret_literals({"notes": "key 0123456789abcdef0123456789abcdef"}, redactor=Redactor())
    assert findings[0].path == "$.notes"


def test_ensure_raises_without_echoing_values() -> None:
    with pytest.raises(SecretIngressError) as excinfo:
        ensure_no_secret_literals({"secretValue": "super-secret-value"}, context="Backend intent")
    assert "super-secret-value" not in str(excinfo.value)
    assert "$.secretValue" in str(excinfo.value)


def test_audit_log_writes_redacted_jsonl(tmp_path: Path) -> None:
    log = AuditLog(tmp_path, session_id="s1", redactor=Redactor())
    log.record(
        "tool.call",
        tool="smoke_test_access",
        outcome=AuditOutcome.OK,
        details={"apiKey": "plain-key-value", "model": "gpt-5.2"},
        now=datetime(2026, 10, 7, tzinfo=UTC),
    )
    lines = (tmp_path / "audit-2026-10-07.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    assert entry["session"] == "s1"
    assert entry["details"]["apiKey"] == REDACTED
    assert entry["details"]["model"] == "gpt-5.2"
    assert "plain-key-value" not in lines[0]
