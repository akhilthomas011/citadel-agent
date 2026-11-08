# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Read immutable hourly interiors and exact event-time edges from one analytics generation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field, ValidationError

from citadel_agent.analytics.rollups import UsageDimension
from citadel_agent.analytics.snapshots import (
    AnalyticsHead,
    AnalyticsStore,
    AnalyticsUnavailable,
    EventRecord,
    PeriodTotals,
    PriceSnapshot,
    RollupRecord,
    SnapshotInfo,
    period_key,
)
from citadel_agent.analytics.views import CUSTOM_DIMENSIONS
from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import DataQuality, DataSource, ObservationState, TimeRange

EVENT_WINDOW_SQL = (
    'SELECT * FROM c WHERE c.kind = "event" '
    "AND c.payload.observation.eventTime >= @start AND c.payload.observation.eventTime < @end ORDER BY c.id"
)
HOUR_WINDOW_SQL = (
    'SELECT * FROM c WHERE c.kind = "hour" '
    "AND c.payload.checkpoint.buckets[0].window.start >= @start "
    "AND c.payload.checkpoint.buckets[0].window.end <= @end ORDER BY c.id"
)


class _Cursor(CitadelModel):
    segment: int = Field(default=0, ge=0, le=3)
    continuation: str | None = None


@dataclass(frozen=True)
class ReadModelPage:
    events: tuple[EventRecord, ...]
    rollups: tuple[RollupRecord, ...]
    continuation: str | None
    records: int


class AnalyticsReader:
    def __init__(
        self,
        store: AnalyticsStore,
        fingerprint: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.fingerprint = fingerprint
        self.clock = clock

    def latest(self) -> SnapshotInfo:
        current = self.store.maybe_get("head")
        if current is None:
            raise AnalyticsUnavailable("Maintained analytics have not published their first generation.")
        head = self.store.payload(current, AnalyticsHead)
        if head.published is None:
            raise AnalyticsUnavailable("Maintained analytics are building their first generation.")
        self._valid(head.published)
        return head.published

    def snapshot(self, generation: str) -> SnapshotInfo:
        from citadel_agent.analytics.snapshots import GenerationState

        version = self.store.maybe_get("generation", generation)
        if version is None:
            raise AnalyticsUnavailable("The requested analytics generation expired.")
        state = self.store.payload(version, GenerationState)
        if state.phase != "ready":
            raise AnalyticsUnavailable("The requested analytics generation is not published.")
        self._valid(state.info)
        return state.info

    def _valid(self, info: SnapshotInfo) -> None:
        if info.source_fingerprint != self.fingerprint:
            raise AnalyticsUnavailable("Maintained analytics target a previous source configuration.")
        if info.expires_at <= self.clock() or info.pricing_snapshot_at is None:
            raise AnalyticsUnavailable("The maintained analytics generation is expired or incomplete.")

    @staticmethod
    def _segments(info: SnapshotInfo, window: TimeRange) -> list[tuple[str, TimeRange]]:
        if window.start < info.window.start:
            raise AnalyticsUnavailable(
                "The requested window precedes maintained retention; use a source scan."
            )
        end = min(window.end, info.window.end)
        if window.start >= end:
            raise AnalyticsUnavailable("Maintained analytics have not captured the requested window.")
        start = window.start
        lower = start.replace(minute=0, second=0, microsecond=0)
        if lower < start:
            lower += timedelta(hours=1)
        upper = end.replace(minute=0, second=0, microsecond=0)
        if lower >= upper:
            return [("event", TimeRange(start=start, end=end))]
        segments = []
        if start < lower:
            segments.append(("event", TimeRange(start=start, end=lower)))
        segments.append(("hour", TimeRange(start=lower, end=upper)))
        if upper < end:
            segments.append(("event", TimeRange(start=upper, end=end)))
        return segments

    def validate_window(self, info: SnapshotInfo, window: TimeRange) -> None:
        self._valid(info)
        self._segments(info, window)

    def request_counts(
        self,
        info: SnapshotInfo,
        window: TimeRange,
        dimension: UsageDimension,
        filters: dict[str, str | None],
        *,
        limit: int = 200,
    ) -> tuple[dict[str | None, int], bool]:
        """Server aggregate of retained event identities; no financial or custom fields are selected."""
        self.validate_window(info, window)
        if dimension in CUSTOM_DIMENSIONS or any(
            name not in {value.value for value in UsageDimension if value not in CUSTOM_DIMENSIONS}
            for name in filters
        ):
            raise ValueError("operational attribution does not expose custom identifiers")
        column = "c.payload.observation.dimensions." + dimension.value
        sql = (
            f'SELECT {column} AS value, COUNT(1) AS requests FROM c WHERE c.kind = "event" '  # noqa: S608
            "AND c.payload.observation.eventTime >= @start AND c.payload.observation.eventTime < @end"
        )
        parameters = [
            {"name": "@start", "value": window.start.isoformat().replace("+00:00", "Z")},
            {"name": "@end", "value": min(window.end, info.window.end).isoformat().replace("+00:00", "Z")},
        ]
        for index, (name, value) in enumerate(filters.items()):
            field = "c.payload.observation.dimensions." + name
            if value is None:
                sql += f" AND IS_NULL({field})"
            else:
                sql += f" AND {field} = @filter{index}"
                parameters.append({"name": f"@filter{index}", "value": value})
        sql += " GROUP BY " + column
        counts = {}
        rows = self.store.container.query_items(
            query=sql,
            parameters=parameters,
            partition_key=self.store.partition(info.generation),
            max_item_count=limit + 1,
        )
        for row in rows:
            label, count = row.get("value"), row.get("requests")
            if (
                (label is not None and not isinstance(label, str))
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
                or label in counts
            ):
                raise AnalyticsUnavailable("Stored operational attribution returned invalid counts.")
            if len(counts) == limit:
                self._valid(info)
                return counts, True
            counts[label] = count
        self._valid(info)
        return counts, False

    def page(self, info: SnapshotInfo, window: TimeRange, continuation: str | None) -> ReadModelPage:
        self._valid(info)
        try:
            cursor = _Cursor.model_validate_json(continuation) if continuation else _Cursor()
        except ValidationError:
            raise AnalyticsUnavailable("The maintained report cursor is invalid.") from None
        segments = self._segments(info, window)
        if cursor.segment >= len(segments):
            raise AnalyticsUnavailable("The maintained report cursor is outside its query.")
        kind, segment = segments[cursor.segment]
        page = self.store.page(
            info.generation,
            EVENT_WINDOW_SQL if kind == "event" else HOUR_WINDOW_SQL,
            {
                "start": segment.start.isoformat().replace("+00:00", "Z"),
                "end": segment.end.isoformat().replace("+00:00", "Z"),
            },
            continuation=cursor.continuation,
        )
        events: list[EventRecord] = []
        rollups: list[RollupRecord] = []
        for raw in page.items:
            record = self.store.decode(raw)
            if record.document.kind != kind:
                raise AnalyticsUnavailable("Maintained analytics returned the wrong data grain.")
            if kind == "event":
                event = self.store.payload(record, EventRecord)
                if event.observation is None or event.cost is None:
                    raise AnalyticsUnavailable("Maintained analytics contain an unfinished event.")
                events.append(event)
            else:
                rollups.append(self.store.payload(record, RollupRecord))
        cursor.continuation = page.continuation_token
        if cursor.continuation is None:
            cursor.segment += 1
        token = cursor.model_dump_json() if cursor.segment < len(segments) else None
        self._valid(info)
        return ReadModelPage(tuple(events), tuple(rollups), token, len(page.items))

    def quality(self, info: SnapshotInfo, window: TimeRange) -> DataQuality:
        self._valid(info)
        warnings = [*info.warnings, "immutable-read-model-generation"]
        watermark = None
        prices = self.store.payload(self.store.get("prices", info.generation), PriceSnapshot)
        if info.rejected_records == 0:
            matches = []
            for proof in prices.proofs:
                if not (
                    proof.scope == self.store.scope
                    and proof.window.start <= window.start < window.end <= proof.window.end
                    and proof.source_fingerprint == info.source_fingerprint
                    and proof.pricing_fingerprint == prices.fingerprint
                    and proof.source_write_cutoff <= info.source_cutoff
                ):
                    continue
                row = self.store.maybe_get("p:" + period_key(proof.window), info.generation)
                if row is not None:
                    totals = self.store.payload(row, PeriodTotals)
                    if totals.event_count == proof.event_count and totals.event_digest == proof.event_digest:
                        matches.append(proof)
            if len(matches) == 1:
                watermark = matches[0].complete_through
        if watermark is None:
            warnings.append("source-watermark-unavailable")
        if info.rejected_records:
            warnings.append("usage-records-quarantined")
        if window.end > info.window.end:
            warnings.append("read-model-refresh-before-window-end")
        stale = self.clock() - info.created_at > timedelta(minutes=30)
        if stale:
            warnings.append("read-model-refresh-stale")
        return DataQuality(
            source=DataSource.COSMOS_USAGE,
            observed_at=info.created_at,
            event_window=window,
            state=ObservationState.STALE
            if stale
            else (
                ObservationState.PARTIAL
                if info.rejected_records or window.end > info.window.end
                else ObservationState.UNKNOWN
                if watermark is None
                else ObservationState.PARTIAL
            ),
            watermark=watermark,
            warning_codes=list(dict.fromkeys(warnings)),
        )


def dimensions_match(dimensions: dict[str, Any], filters: dict[str, str | None]) -> bool:
    return all(dimensions.get(name) == value for name, value in filters.items())
