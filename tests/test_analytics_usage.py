"""Upstream usage records normalize with stable identity and explicit timestamp rules."""

from datetime import UTC, datetime

import pytest

from citadel_agent.analytics.usage import (
    UsageNormalizationError,
    UsageSourceKey,
    normalize_usage_document,
    parse_upstream_timestamp,
)
from citadel_agent.portal.contracts import HubScope

TENANT_ID = "11111111-1111-4111-8111-111111111111"
SCOPE = HubScope(
    tenant_id=TENANT_ID,
    hub_id="citadel-production",
    environment="prod",
    subscription_id="22222222-2222-4222-8222-222222222222",
)


def _source_key(
    *,
    account: str = "usage-account",
    container: str = "llm-usage-container",
    partition_key: str = "partition-a",
    document_id: str = "llm-event-01",
) -> UsageSourceKey:
    return UsageSourceKey.create(
        account=account,
        database="usage-db",
        container=container,
        partition_key=partition_key,
        document_id=document_id,
    )


def _document(**overrides) -> dict[str, object]:
    return {
        "id": "llm-event-01",
        "timestamp": "7/19/2026 11:00:00 AM",
        "appId": "billing-app",
        "productName": "finance-chat",
        "deploymentName": "chat",
        "model": "gpt-model",
        "backendId": "backend-a",
        "gatewayName": "gateway-a",
        "gatewayRegion": "Sweden Central",
        "customDimension1": "cost-center-12",
        "customDimension2": "NA",
        "targetService": "chat.completion",
        "operationName": "Create completion",
        "aiGatewayId": "managed",
        "promptTokens": "1000",
        "responseTokens": "500",
        "totalTokens": "1500",
        "promptCachedTokens": "200",
        "promptAudioTokens": "0",
        "completionAudioTokens": "0",
        "completionReasoningTokens": "0",
        "completionAcceptedPredictionTokens": "0",
        "completionRejectedPredictionTokens": "0",
        "EventEnqueuedUtcTime": "2026-07-19T11:02:00.000Z",
        **overrides,
    }


def test_upstream_display_timestamp_is_parsed_as_utc() -> None:
    assert parse_upstream_timestamp("7/19/2026 11:00:00 AM") == datetime(
        2026,
        7,
        19,
        11,
        tzinfo=UTC,
    )
    assert parse_upstream_timestamp("2026-07-19T13:00:00+02:00") == datetime(
        2026,
        7,
        19,
        11,
        tzinfo=UTC,
    )


@pytest.mark.parametrize(
    "timestamp",
    ["", "2026-07-19T11:00:00", "July 19 2026", "19/07/2026 11:00:00"],
)
def test_ambiguous_or_unknown_timestamp_formats_are_quarantined(timestamp: str) -> None:
    with pytest.raises(UsageNormalizationError, match="timestamp"):
        parse_upstream_timestamp(timestamp)


def test_normalizer_maps_known_fields_and_keeps_ingestion_time_distinct() -> None:
    event = normalize_usage_document(_document(), source_key=_source_key(), scope=SCOPE)

    assert event.event_id == _source_key().event_id
    assert event.event_time == datetime(2026, 7, 19, 11, tzinfo=UTC)
    assert event.ingested_at == datetime(2026, 7, 19, 11, 2, tzinfo=UTC)
    assert event.scope == SCOPE
    assert event.dimensions.model == "gpt-model"
    assert event.dimensions.deployment == "chat"
    assert event.dimensions.product == "finance-chat"
    assert event.dimensions.custom_dimension_1 == "cost-center-12"
    assert event.dimensions.custom_dimension_2 is None
    assert event.usage.prompt == 1000
    assert event.usage.cached_prompt == 200
    assert event.usage.total == 1500
    assert "promptTokens" in event.available_usage_fields


def test_missing_total_is_not_inferred_and_missing_subcounts_stay_unavailable() -> None:
    document = _document()
    for field in ("totalTokens", "promptCachedTokens", "promptAudioTokens"):
        document.pop(field)

    event = normalize_usage_document(document, source_key=_source_key(), scope=SCOPE)

    assert event.usage.total is None
    assert event.usage.cached_prompt == 0
    assert "promptCachedTokens" not in event.available_usage_fields
    assert "promptAudioTokens" not in event.available_usage_fields


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"id": "wrong-document"}, "source-identity-mismatch"),
        ({"promptTokens": "1.5"}, "not-an-integer"),
        ({"responseTokens": -1}, "negative"),
        ({"promptCachedTokens": "1001"}, "cached-count-exceeds-prompt"),
        ({"promptAudioTokens": "1001"}, "subtype-exceeds-total"),
    ],
)
def test_malformed_documents_are_rejected_with_field_context(changes, code: str) -> None:
    with pytest.raises(UsageNormalizationError) as caught:
        normalize_usage_document(
            _document(**changes),
            source_key=_source_key(),
            scope=SCOPE,
        )
    assert caught.value.code == code


def test_source_key_hash_includes_partition_and_container_identity() -> None:
    key = _source_key()

    assert key.event_id != _source_key(partition_key="partition-b").event_id
    assert key.event_id != _source_key(container="agent-usage-container").event_id
