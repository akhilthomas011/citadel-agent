# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Compact six-view projections from one report input, with exact money and token completeness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from pydantic import Field

from citadel_agent.analytics.pricing import UsageCost
from citadel_agent.analytics.rollups import (
    FinOpsCheckpoint,
    IncrementalFinOps,
    RollupGrain,
    UsageDimension,
)
from citadel_agent.analytics.usage import UsageObservation
from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import DataQuality, HubScope, MonetaryAmount, TimeRange, TokenUsage

CUSTOM_DIMENSIONS = (UsageDimension.CUSTOM_DIMENSION_1, UsageDimension.CUSTOM_DIMENSION_2)
STANDARD_DIMENSIONS = tuple(value for value in UsageDimension if value not in CUSTOM_DIMENSIONS)
_PROFILES: dict[str, tuple[RollupGrain, tuple[UsageDimension, ...]]] = {
    **{value.value: (RollupGrain.WINDOW, (value,)) for value in STANDARD_DIMENSIONS},
    "product_deployment": (RollupGrain.WINDOW, (UsageDimension.PRODUCT, UsageDimension.DEPLOYMENT)),
    "model_backend": (RollupGrain.WINDOW, (UsageDimension.MODEL, UsageDimension.BACKEND)),
    "region_gateway": (RollupGrain.WINDOW, (UsageDimension.REGION, UsageDimension.GATEWAY)),
    "gateway_application": (RollupGrain.WINDOW, (UsageDimension.GATEWAY, UsageDimension.APPLICATION)),
    "daily": (RollupGrain.DAY, ()),
    "hourly": (RollupGrain.HOUR, ()),
    "deployment_daily": (RollupGrain.DAY, (UsageDimension.DEPLOYMENT,)),
}


class AnalyticalRow(CitadelModel):
    window: TimeRange
    dimensions: dict[UsageDimension, str | None]
    requests: int = Field(ge=0)
    unpriced_requests: int = Field(ge=0)
    usage: TokenUsage
    unavailable_tokens: list[str]
    costs: list[MonetaryAmount]
    priced_usage_percent: Decimal = Field(ge=0, le=100)
    hour_utc: int | None = Field(default=None, ge=0, le=23)


class FinOpsAnalysis(CitadelModel):
    profiles: dict[str, list[AnalyticalRow]]
    custom_dimensions: list[UsageDimension] = Field(default_factory=list)
    hour_of_day: list[AnalyticalRow] = Field(default_factory=list)


class AnalysisCheckpoint(CitadelModel):
    profiles: dict[str, FinOpsCheckpoint]


class AnalyticalAccumulator:
    def __init__(
        self, scope: HubScope, window: TimeRange, custom_dimensions: tuple[UsageDimension, ...] = ()
    ) -> None:
        if any(value not in CUSTOM_DIMENSIONS for value in custom_dimensions):
            raise ValueError("only configured custom dimensions can extend analytical profiles")
        self.custom_dimensions = custom_dimensions
        self.definitions = {
            **_PROFILES,
            **{value.value: (RollupGrain.WINDOW, (value,)) for value in custom_dimensions},
        }
        self.profiles = {
            name: IncrementalFinOps(scope, window, dimensions, grain=grain)
            for name, (grain, dimensions) in self.definitions.items()
        }

    def restore(self, checkpoint: AnalysisCheckpoint) -> None:
        if set(checkpoint.profiles) != set(self.profiles):
            raise ValueError("analytical checkpoint profiles do not match the authorized request")
        for name, saved in checkpoint.profiles.items():
            expected = self.profiles[name]
            if (
                saved.scope != expected.scope
                or saved.window != expected.window
                or saved.group_by != expected.group_by
                or saved.grain != expected.grain
            ):
                raise ValueError("analytical checkpoint scope, window, grain or dimensions are invalid")
            self.profiles[name] = IncrementalFinOps.restore(saved)

    def checkpoint(self) -> AnalysisCheckpoint:
        return AnalysisCheckpoint(
            profiles={name: value.checkpoint() for name, value in self.profiles.items()}
        )

    @property
    def bucket_count(self) -> int:
        return sum(value.bucket_count for value in self.profiles.values())

    def add(self, events: Sequence[UsageObservation], costs: Mapping[str, UsageCost]) -> None:
        for value in self.profiles.values():
            value.add(events, costs)

    def merge(self, checkpoint: FinOpsCheckpoint) -> None:
        for value in self.profiles.values():
            value.merge(checkpoint)

    def result(self, quality: DataQuality) -> FinOpsAnalysis:
        profiles = {}
        for name, value in self.profiles.items():
            checkpoint = value.checkpoint()
            aggregates = value.results(quality)
            profiles[name] = [
                AnalyticalRow(
                    window=row.window,
                    dimensions=dict(zip(checkpoint.group_by, bucket.dimensions, strict=True)),
                    requests=bucket.requests,
                    unpriced_requests=bucket.unpriced_requests,
                    usage=row.usage,
                    unavailable_tokens=[
                        warning.removeprefix("usage-field-unavailable:")
                        for warning in sorted(bucket.usage_warning_codes)
                    ],
                    costs=row.costs,
                    priced_usage_percent=row.pricing_coverage.priced_usage_percent,
                )
                for row, bucket in zip(aggregates, checkpoint.buckets, strict=True)
            ]
        hourly = self.profiles["hourly"].checkpoint()
        hours: dict[int, IncrementalFinOps] = {}
        for bucket in hourly.buckets:
            hour = bucket.window.start.hour
            accumulator = hours.setdefault(
                hour, IncrementalFinOps(hourly.scope, hourly.window, (), grain=RollupGrain.WINDOW)
            )
            accumulator.merge(hourly.model_copy(update={"buckets": [bucket]}))
        by_hour = []
        for hour, value in sorted(hours.items()):
            row = value.results(quality)[0]
            bucket = value.checkpoint().buckets[0]
            by_hour.append(
                AnalyticalRow(
                    window=hourly.window,
                    dimensions={},
                    hour_utc=hour,
                    requests=bucket.requests,
                    unpriced_requests=bucket.unpriced_requests,
                    usage=row.usage,
                    costs=row.costs,
                    priced_usage_percent=row.pricing_coverage.priced_usage_percent,
                    unavailable_tokens=[
                        warning.removeprefix("usage-field-unavailable:")
                        for warning in sorted(bucket.usage_warning_codes)
                    ],
                )
            )
        return FinOpsAnalysis(
            profiles=profiles, custom_dimensions=list(self.custom_dimensions), hour_of_day=by_hour
        )
