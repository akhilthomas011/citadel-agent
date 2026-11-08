# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for scoped Cosmos query cursors."""

from datetime import UTC, datetime, timedelta

import pytest

from citadel_agent.analytics.cursors import (
    CursorValidationError,
    ScopedCosmosCursorSigner,
    cosmos_query_fingerprint,
)
from citadel_agent.portal.contracts import HubScope

SCOPE = HubScope(
    tenant_id="11111111-1111-4111-8111-111111111111",
    hub_id="citadel-production",
    environment="prod",
    subscription_id="22222222-2222-4222-8222-222222222222",
)
OTHER_SCOPE = SCOPE.model_copy(update={"environment": "test"})
NOW = datetime(2026, 7, 19, 11, tzinfo=UTC)
QUERY = "SELECT * FROM c WHERE c.productName = @product"
ACCOUNT_ENDPOINT = "https://acct.documents.azure.com:443/"
DATABASE = "usage-db"
CONTAINER = "llm-usage-container"
FINGERPRINT = cosmos_query_fingerprint(
    QUERY,
    {"product": "P"},
    account_endpoint=ACCOUNT_ENDPOINT,
    database=DATABASE,
    container=CONTAINER,
    page_size=100,
)


def test_query_fingerprint_normalizes_parameter_names_and_order() -> None:
    first = cosmos_query_fingerprint(
        QUERY,
        {"product": "P", "deployment": "chat"},
        account_endpoint=ACCOUNT_ENDPOINT,
        database=DATABASE,
        container=CONTAINER,
        page_size=100,
    )
    second = cosmos_query_fingerprint(
        QUERY,
        {"@deployment": "chat", "@product": "P"},
        account_endpoint=ACCOUNT_ENDPOINT,
        database=DATABASE,
        container=CONTAINER,
        page_size=100,
    )

    assert first == second
    assert first != cosmos_query_fingerprint(
        QUERY,
        {"product": "P", "deployment": "chat"},
        account_endpoint=ACCOUNT_ENDPOINT,
        database=DATABASE,
        container=CONTAINER,
        page_size=200,
    )
    assert first != cosmos_query_fingerprint(
        QUERY,
        {"product": "P", "deployment": "chat"},
        account_endpoint=ACCOUNT_ENDPOINT,
        database=DATABASE,
        container="model-pricing",
        page_size=100,
    )


def test_query_fingerprint_rejects_duplicate_or_non_json_parameters() -> None:
    with pytest.raises(ValueError, match="unique"):
        cosmos_query_fingerprint(
            QUERY,
            {"name": "a", "@name": "b"},
            account_endpoint=ACCOUNT_ENDPOINT,
            database=DATABASE,
            container=CONTAINER,
            page_size=100,
        )
    with pytest.raises(TypeError):
        cosmos_query_fingerprint(
            QUERY,
            {"product": object()},
            account_endpoint=ACCOUNT_ENDPOINT,
            database=DATABASE,
            container=CONTAINER,
            page_size=100,
        )


def test_signed_cursor_round_trips_only_for_its_scope_and_query() -> None:
    signer = ScopedCosmosCursorSigner(b"c" * 32, ttl_seconds=600)
    cursor = signer.issue(
        "cosmos-continuation",
        scope=SCOPE,
        query_fingerprint=FINGERPRINT,
        now=NOW,
    )

    assert (
        signer.read(
            cursor,
            scope=SCOPE,
            query_fingerprint=FINGERPRINT,
            now=NOW + timedelta(seconds=30),
        )
        == "cosmos-continuation"
    )
    with pytest.raises(CursorValidationError, match="scope-mismatch"):
        signer.read(cursor, scope=OTHER_SCOPE, query_fingerprint=FINGERPRINT, now=NOW)
    with pytest.raises(CursorValidationError, match="query-mismatch"):
        signer.read(cursor, scope=SCOPE, query_fingerprint="0" * 64, now=NOW)
    with pytest.raises(CursorValidationError, match="expired"):
        signer.read(
            cursor,
            scope=SCOPE,
            query_fingerprint=FINGERPRINT,
            now=NOW + timedelta(seconds=600),
        )


def test_signed_cursor_rejects_tampering_and_weak_keys() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        ScopedCosmosCursorSigner(b"short")

    signer = ScopedCosmosCursorSigner(b"s" * 32)
    cursor = signer.issue(
        "cosmos-continuation",
        scope=SCOPE,
        query_fingerprint=FINGERPRINT,
        now=NOW,
    )
    encoded_payload, signature = cursor.split(".")
    changed_payload = ("A" if encoded_payload[0] != "A" else "B") + encoded_payload[1:]

    with pytest.raises(CursorValidationError, match="signature-invalid"):
        signer.read(
            f"{changed_payload}.{signature}",
            scope=SCOPE,
            query_fingerprint=FINGERPRINT,
            now=NOW,
        )
