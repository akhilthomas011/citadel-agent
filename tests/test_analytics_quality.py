# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for freshness and completeness reporting."""

from datetime import UTC, datetime, timedelta

import pytest

from citadel_agent.analytics.quality import assess_usage_quality
from citadel_agent.portal.contracts import DataSource, ObservationState, TimeRange

OBSERVED_AT = datetime(2026, 8, 1, 1, tzinfo=UTC)
WINDOW = TimeRange(
    start=datetime(2026, 7, 1, tzinfo=UTC),
    end=datetime(2026, 8, 1, tzinfo=UTC),
)


def test_complete_fresh_empty_window_is_healthy() -> None:
    result = assess_usage_quality(
        window=WINDOW,
        observed_at=OBSERVED_AT,
        watermark=WINDOW.end,
        max_watermark_age=timedelta(hours=2),
        events=[],
    )

    assert result.source is DataSource.COSMOS_USAGE
    assert result.state is ObservationState.HEALTHY
    assert result.warning_codes == []


def test_missing_watermark_is_unknown_not_healthy() -> None:
    result = assess_usage_quality(
        window=WINDOW,
        observed_at=OBSERVED_AT,
        watermark=None,
        max_watermark_age=timedelta(hours=2),
        events=[],
    )

    assert result.state is ObservationState.UNKNOWN
    assert result.warning_codes == ["source-watermark-unavailable"]


def test_incomplete_window_and_quarantined_records_are_partial() -> None:
    result = assess_usage_quality(
        window=WINDOW,
        observed_at=OBSERVED_AT,
        watermark=datetime(2026, 7, 31, tzinfo=UTC),
        max_watermark_age=timedelta(days=2),
        events=[],
        rejected_records=2,
    )

    assert result.state is ObservationState.PARTIAL
    assert result.warning_codes == [
        "source-watermark-before-window-end",
        "usage-records-quarantined",
    ]


def test_stale_watermark_is_not_reported_as_healthy_or_partial() -> None:
    result = assess_usage_quality(
        window=WINDOW,
        observed_at=OBSERVED_AT,
        watermark=datetime(2026, 7, 31, tzinfo=UTC),
        max_watermark_age=timedelta(minutes=30),
        events=[],
    )

    assert result.state is ObservationState.STALE
    assert result.warning_codes == ["source-watermark-stale"]


@pytest.mark.parametrize(
    ("observed_at", "max_watermark_age", "watermark"),
    [
        (datetime(2026, 8, 1, 1), timedelta(hours=1), WINDOW.end),
        (OBSERVED_AT, timedelta(0), WINDOW.end),
        (OBSERVED_AT, timedelta(hours=1), datetime(2026, 8, 1, 2, tzinfo=UTC)),
    ],
)
def test_quality_inputs_require_valid_utc_and_age(
    observed_at: datetime,
    max_watermark_age: timedelta,
    watermark: datetime,
) -> None:
    with pytest.raises(ValueError):
        assess_usage_quality(
            window=WINDOW,
            observed_at=observed_at,
            watermark=watermark,
            max_watermark_age=max_watermark_age,
            events=[],
        )
