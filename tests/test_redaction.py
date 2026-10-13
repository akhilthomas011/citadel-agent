# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for secret redaction."""

from __future__ import annotations

import pytest

from citadel_agent.safety.redaction import REDACTED, Redactor

APIM_KEY = "0123456789abcdef0123456789abcdef"
ROTATION_KEY = "f" * 64
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJhdWQiOiJhcGk6Ly94In0.c2lnbmF0dXJlLXZhbHVl"


@pytest.fixture
def redactor() -> Redactor:
    return Redactor()


@pytest.mark.parametrize(
    "text",
    [
        f"key={APIM_KEY}",
        f"rotation {ROTATION_KEY} done",
        f"token {JWT}",
        "Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=x;SharedAccessKey=abcDEF123+/=",
        "https://acct.blob.core.windows.net/c?sv=2024&sig=abcdEFGH1234%2B&se=2026",
        "AKIAABCDEFGHIJKLMNOP",
    ],
)
def test_secret_shapes_are_redacted(redactor: Redactor, text: str) -> None:
    redacted = redactor.redact(text)
    assert REDACTED in redacted
    for secret in (APIM_KEY, ROTATION_KEY, JWT, "abcDEF123", "abcdEFGH1234", "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in redacted


def test_bearer_prefix_is_kept(redactor: Redactor) -> None:
    assert redactor.redact("Authorization header Bearer abcdefghijklmnopqrstuvwxyz012345") == (
        f"Authorization header Bearer {REDACTED}"
    )


def test_header_lines_are_redacted(redactor: Redactor) -> None:
    text = "api-key: secret-value-123\nContent-Type: application/json"
    assert redactor.redact(text) == f"api-key: {REDACTED}\nContent-Type: application/json"


def test_registered_secret_is_replaced_exactly(redactor: Redactor) -> None:
    redactor.register_secret("not-shaped-like-a-key-but-secret")
    assert redactor.redact("value=not-shaped-like-a-key-but-secret!") == f"value={REDACTED}!"


@pytest.mark.parametrize(
    "text",
    [
        "11111111-1111-1111-1111-111111111111",  # GUID with dashes
        "23fbc8fe7f2f068de3ed3c80da2344760faac1e6",  # 40-hex commit SHA
        "sha256:" + "a" * 64,  # agent content hash
        "gpt-5.2 on apim-dvfwtaj5al46e",
    ],
)
def test_non_secrets_are_left_alone(redactor: Redactor, text: str) -> None:
    assert redactor.redact(text) == text


def test_redact_obj_handles_sensitive_keys_and_nesting(redactor: Redactor) -> None:
    data = {
        "apiKey": "anything",
        "api-key": "anything",
        "nested": [{"secretValue": "x"}, {"name": "gpt-5.2"}],
        "note": f"key {APIM_KEY}",
        "empty": {"apiKey": ""},
    }
    result = redactor.redact_obj(data)
    assert result["apiKey"] == REDACTED
    assert result["api-key"] == REDACTED
    assert result["nested"][0]["secretValue"] == REDACTED
    assert result["nested"][1]["name"] == "gpt-5.2"
    assert APIM_KEY not in result["note"]
    assert result["empty"]["apiKey"] == ""
