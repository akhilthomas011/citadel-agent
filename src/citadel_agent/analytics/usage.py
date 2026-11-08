# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Normalize upstream Cosmos usage documents without treating missing data as zero."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import Field, field_validator

from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import HubScope, TokenUsage, UsageDimensions

_USAGE_TIMESTAMP_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p")
_DIGITS = re.compile(r"^[0-9]+$")
_OPTIONAL_TOKEN_FIELDS = {
    "totalTokens": "total",
    "promptCachedTokens": "cached_prompt",
    "promptAudioTokens": "prompt_audio",
    "completionAudioTokens": "completion_audio",
    "completionReasoningTokens": "completion_reasoning",
    "completionAcceptedPredictionTokens": "completion_accepted_prediction",
    "completionRejectedPredictionTokens": "completion_rejected_prediction",
    "promptImageTokens": "prompt_image",
    "promptCachedImageTokens": "cached_prompt_image",
}


class UsageNormalizationError(ValueError):
    """A source record is malformed or ambiguous and should be quarantined."""

    def __init__(self, code: str, field: str) -> None:
        super().__init__(f"Usage record field '{field}' could not be normalized ({code}).")
        self.code = code
        self.field = field


class UsageSourceKey(CitadelModel):
    """Stable source identity needed for idempotent backfill and change-feed updates."""

    account: str = Field(min_length=1, max_length=128)
    database: str = Field(min_length=1, max_length=128)
    container: str = Field(min_length=1, max_length=128)
    partition_key_json: str = Field(min_length=1, max_length=2048)
    document_id: str = Field(min_length=1, max_length=255)

    @classmethod
    def create(
        cls,
        *,
        account: str,
        database: str,
        container: str,
        partition_key: str | int | float | bool | None,
        document_id: str,
    ) -> UsageSourceKey:
        try:
            encoded_partition_key = json.dumps(
                partition_key,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("partition key must be a JSON scalar") from exc
        if isinstance(partition_key, (dict, list)):
            raise ValueError("partition key must be a JSON scalar")
        return cls(
            account=account,
            database=database,
            container=container,
            partition_key_json=encoded_partition_key,
            document_id=document_id,
        )

    @property
    def event_id(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class UsageObservation(CitadelModel):
    """Accounting facts safe to persist without a source document ID or partition value."""

    schema_version: int = 1
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: HubScope
    event_time: datetime
    ingested_at: datetime | None = None
    dimensions: UsageDimensions
    usage: TokenUsage
    available_usage_fields: frozenset[str]
    warning_codes: list[str] = Field(default_factory=list)

    @field_validator("event_time", "ingested_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("normalized usage timestamps must include a timezone")
            return value.astimezone(UTC)
        return value


class NormalizedUsageEvent(UsageObservation):
    """Transient normalized source event; source coordinates are not part of the read model."""

    source_key: UsageSourceKey


def normalize_usage_document(
    document: dict[str, Any],
    *,
    source_key: UsageSourceKey,
    scope: HubScope,
) -> NormalizedUsageEvent:
    """Map the upstream llm-usage-container schema to the shared, typed event contract."""
    document_id = document.get("id")
    if not isinstance(document_id, str) or document_id != source_key.document_id:
        raise UsageNormalizationError("source-identity-mismatch", "id")

    raw_time = document.get("timestamp")
    if not isinstance(raw_time, str):
        raise UsageNormalizationError("missing-or-invalid", "timestamp")
    event_time = parse_upstream_timestamp(raw_time)

    available_fields: set[str] = set()
    token_values: dict[str, int | None] = {}
    for source_name, target_name in (
        ("promptTokens", "prompt"),
        ("responseTokens", "completion"),
        *_OPTIONAL_TOKEN_FIELDS.items(),
    ):
        if source_name not in document:
            if source_name in ("promptTokens", "responseTokens"):
                raise UsageNormalizationError("missing", source_name)
            token_values[target_name] = None if target_name == "total" else 0
            continue
        token_values[target_name] = _token_count(document[source_name], source_name)
        available_fields.add(source_name)

    prompt = token_values["prompt"]
    completion = token_values["completion"]
    cached_prompt = token_values["cached_prompt"]
    if prompt is None or completion is None or cached_prompt is None:
        raise RuntimeError("Required token values were not normalized.")
    if cached_prompt > prompt:
        raise UsageNormalizationError("cached-count-exceeds-prompt", "promptCachedTokens")
    for source_field, count, total in (
        ("promptAudioTokens", token_values["prompt_audio"], prompt),
        ("promptImageTokens", token_values["prompt_image"], prompt),
        ("completionAudioTokens", token_values["completion_audio"], completion),
        (
            "completionReasoningTokens",
            token_values["completion_reasoning"],
            completion,
        ),
    ):
        if count is not None and count > total:
            raise UsageNormalizationError("subtype-exceeds-total", source_field)
    if token_values["cached_prompt_image"] > token_values["prompt_image"]:
        raise UsageNormalizationError("cached-image-exceeds-image", "promptCachedImageTokens")

    model = _source_text(document, "model") or _source_text(document, "deploymentName")
    if model is None:
        raise UsageNormalizationError("model-unavailable", "model")

    ingestion_time = _optional_ingestion_time(document)
    warnings = ["ingestion-time-unavailable"] if ingestion_time is None else []
    usage = TokenUsage(**token_values)
    dimensions = UsageDimensions(
        model=model,
        deployment=_source_text(document, "deploymentName"),
        backend=_source_text(document, "backendId"),
        product=_source_text(document, "productName"),
        application=_source_text(document, "appId"),
        region=_source_text(document, "gatewayRegion"),
        service_type=_source_text(document, "targetService"),
        gateway=_source_text(document, "gatewayName"),
        custom_dimension_1=_source_text(document, "customDimension1"),
        custom_dimension_2=_source_text(document, "customDimension2"),
        operation_name=_source_text(document, "operationName"),
        ai_gateway_id=_source_text(document, "aiGatewayId"),
    )
    return NormalizedUsageEvent(
        event_id=source_key.event_id,
        source_key=source_key,
        scope=scope,
        event_time=event_time,
        ingested_at=ingestion_time,
        dimensions=dimensions,
        usage=usage,
        available_usage_fields=frozenset(available_fields),
        warning_codes=warnings,
    )


def parse_upstream_timestamp(value: str) -> datetime:
    """Parse ISO timestamps or upstream's documented UTC Kusto display format."""
    text = value.strip()
    if not text:
        raise UsageNormalizationError("empty", "timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for timestamp_format in _USAGE_TIMESTAMP_FORMATS:
            try:
                parsed = datetime.strptime(text, timestamp_format)
                break
            except ValueError:
                continue
        if parsed is None:
            raise UsageNormalizationError("unrecognized-format", "timestamp") from None
        return parsed.replace(tzinfo=UTC)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UsageNormalizationError("timezone-missing", "timestamp")
    return parsed.astimezone(UTC)


def _token_count(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise UsageNormalizationError("not-an-integer", field_name)
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and _DIGITS.fullmatch(value.strip()):
        result = int(value.strip())
    else:
        raise UsageNormalizationError("not-an-integer", field_name)
    if result < 0:
        raise UsageNormalizationError("negative", field_name)
    return result


def _source_text(document: dict[str, Any], field_name: str) -> str | None:
    value = document.get(field_name)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized.upper() == "NA":
        return None
    return normalized


def _optional_ingestion_time(document: dict[str, Any]) -> datetime | None:
    for field_name in ("EventEnqueuedUtcTime", "EventProcessedUtcTime"):
        value = document.get(field_name)
        if value is None:
            continue
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        return parsed.astimezone(UTC)
    return None
