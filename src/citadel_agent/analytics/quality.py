# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Freshness and completeness assessment for normalized usage windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from citadel_agent.analytics.usage import NormalizedUsageEvent
from citadel_agent.portal.contracts import DataQuality, DataSource, ObservationState, TimeRange


def assess_usage_quality(
    *,
    window: TimeRange,
    observed_at: datetime,
    watermark: datetime | None,
    max_watermark_age: timedelta,
    events: list[NormalizedUsageEvent],
    rejected_records: int = 0,
) -> DataQuality:
    """Report whether usage for a requested window is complete, fresh, and parseable."""
    observed = _utc(observed_at, "observed_at")
    normalized_watermark = _utc(watermark, "watermark") if watermark is not None else None
    if max_watermark_age <= timedelta(0):
        raise ValueError("max_watermark_age must be greater than zero")
    if rejected_records < 0:
        raise ValueError("rejected_records must not be negative")
    if normalized_watermark is not None and normalized_watermark > observed:
        raise ValueError("watermark must not be later than observed_at")

    warnings: list[str] = []
    if normalized_watermark is None:
        warnings.append("source-watermark-unavailable")
        state = ObservationState.UNKNOWN
    else:
        stale = observed - normalized_watermark > max_watermark_age
        complete = normalized_watermark >= window.end
        if stale:
            warnings.append("source-watermark-stale")
            state = ObservationState.STALE
        elif not complete:
            warnings.append("source-watermark-before-window-end")
            state = ObservationState.PARTIAL
        else:
            state = ObservationState.HEALTHY

    if rejected_records:
        warnings.append("usage-records-quarantined")
        if state is ObservationState.HEALTHY:
            state = ObservationState.PARTIAL
    for event in events:
        warnings.extend(event.warning_codes)
    if warnings and state is ObservationState.HEALTHY:
        state = ObservationState.PARTIAL

    return DataQuality(
        source=DataSource.COSMOS_USAGE,
        observed_at=observed,
        event_window=window,
        state=state,
        watermark=normalized_watermark,
        warning_codes=warnings,
    )


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)
