"""Maintained analytics publish immutable, scoped generations without replay double-counting."""

import copy
import json
from datetime import timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace

import pytest
from azure.core.exceptions import AzureError
from azure.core.paging import ItemPaged
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosBatchOperationError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from citadel_agent.analytics.materialize import AnalyticsMaterializer
from citadel_agent.analytics.pricing import ModelPricingEntry, PricingStatus
from citadel_agent.analytics.rollups import IncrementalFinOps, UsageDimension
from citadel_agent.analytics.snapshots import (
    AnalyticsConflict,
    AnalyticsHead,
    AnalyticsStore,
    EventRecord,
    GenerationState,
    PeriodCompletenessProof,
    RollupRecord,
    add_digest,
    pricing_fingerprint,
    source_fingerprint,
)
from citadel_agent.analytics.usage import UsageObservation
from citadel_agent.azure_clients.cosmos import CosmosQueryPage
from citadel_agent.portal.contracts import TimeRange
from citadel_agent.portal.data import PortalDataService
from test_portal_product import NOW, usage_document
from test_portal_product import config as config_fixture

config = config_fixture


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class AnalyticsContainer:
    def __init__(self):
        self.items = {}
        self.revision = 0
        self.indexes = {}
        self.lose_ack = False

    def _write(self, body):
        key = (body["partitionKey"], body["id"])
        previous = self.items.get(key)
        if previous is None or previous["kind"] != body["kind"]:
            self.indexes = {
                item: value
                for item, value in self.indexes.items()
                if item[0] != key[0] or (f'"{body["kind"]}"' not in item[1] and "kind" not in dict(item[2]))
            }
        self.revision += 1
        self.items[key] = {**copy.deepcopy(body), "_etag": str(self.revision)}
        return copy.deepcopy(self.items[key])

    def create_item(self, body):
        if (body["partitionKey"], body["id"]) in self.items:
            raise CosmosResourceExistsError(status_code=409)
        return self._write(body)

    def read_item(self, item, partition_key):
        try:
            return copy.deepcopy(self.items[partition_key, item])
        except KeyError:
            raise CosmosResourceNotFoundError(status_code=404) from None

    def replace_item(self, item, body, *, partition_key, etag, match_condition):
        current = self.read_item(item, partition_key)
        if current["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        return self._write(body)

    def execute_item_batch(self, operations, partition_key):
        assert len(operations) <= 100
        assert len(json.dumps(operations).encode()) < 1_200_000
        for operation, args, *options in operations:
            if operation == "replace":
                actual = self.items.get((partition_key, args[0]))
                if actual is None or actual["_etag"] != options[0]["if_match_etag"]:
                    raise CosmosBatchOperationError(
                        headers={},
                        status_code=424,
                        operation_responses=[{"statusCode": 412}],
                        message="Conditional batch fixture.",
                    )
        for operation, args, *_ in operations:
            body = args[0] if operation == "upsert" else args[1]
            assert body["partitionKey"] == partition_key
            self._write(body)
        if self.lose_ack:
            self.lose_ack = False
            raise CosmosAccessConditionFailedError(status_code=503, message="Lost acknowledgement fixture.")

    def query_items(self, query, *, partition_key, parameters, max_item_count):
        values = {value["name"].removeprefix("@"): value["value"] for value in parameters}
        cache_key = (partition_key, query, tuple(sorted(values.items())))
        if cache_key not in self.indexes:
            rows = []
            for key, value in self.items.items():
                if key[0] != partition_key:
                    continue
                if 'IN ("event", "quarantine")' in query:
                    if value["kind"] not in {"event", "quarantine"}:
                        continue
                elif (
                    ('c.kind = "event"' in query and value["kind"] != "event")
                    or ('c.kind = "hour"' in query and value["kind"] != "hour")
                    or ("kind" in values and value["kind"] != values["kind"])
                ):
                    continue
                if "start" in values:
                    payload = value["payload"]
                    instant = (
                        payload["observation"]["eventTime"]
                        if value["kind"] == "event"
                        else payload["checkpoint"]["buckets"][0]["window"]["start"]
                    )
                    if not values["start"] <= instant < values["end"]:
                        continue
                rows.append(key)
            self.indexes[cache_key] = sorted(rows)
        keys = self.indexes[cache_key]

        def extract(token):
            offset = int(token or 0)
            selected = keys[offset : offset + max_item_count]
            following = offset + len(selected)
            continuation = str(following) if following < len(keys) else None
            return continuation, [copy.deepcopy(self.items[key]) for key in selected]

        return ItemPaged(lambda token: token, extract)


class Source:
    def __init__(self, total=100):
        self.documents = {
            str(index): usage_document(
                id=str(index),
                timestamp=(NOW - timedelta(hours=1)).isoformat(),
                _ts=int((NOW - timedelta(seconds=10)).timestamp()),
            )
            for index in range(total)
        }
        self.prices = []
        self.proofs = []
        self.calls = 0
        self.hook = None

    def read_feed_ranges(self, database, container):
        return ({"opaque-test-range": "all"},)

    def query_page(
        self, database, container, sql, parameters, *, page_size, continuation_token, feed_range=None
    ):
        if container == "model-pricing":
            return CosmosQueryPage(tuple(self.prices), None)
        if container == "trusted-completeness":
            return CosmosQueryPage(tuple(self.proofs), None)
        assert "@gateway" in sql and parameters["gateway"] == "test-apim"
        self.calls += 1
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if continuation_token is None:
            self._rows = [
                value
                for value in self.documents.values()
                if parameters.get("floor", 0) <= value["_ts"] <= parameters["cutoff"]
            ]
        rows = self._rows
        offset = int(continuation_token or 0)
        end = min(offset + page_size, len(rows))
        return CosmosQueryPage(tuple(rows[offset:end]), str(end) if end < len(rows) else None)


def setup(config, source, clock=None):
    clock = clock or Clock()
    container = AnalyticsContainer()
    store = AnalyticsStore(container, config.scope, clock=clock)
    data = PortalDataService(config, SimpleNamespace(), usage_reader=source, logs=SimpleNamespace())
    return store, data, AnalyticsMaterializer(store, data, clock=clock), clock


def finish(worker, *, full=False):
    for index in range(25_000):
        result = worker.step(force_full=full and index == 0)
        if result in {"published", "failed", "busy", "idle"}:
            assert result == "published", result
            return worker.store.payload(worker.store.get("head"), AnalyticsHead).published
    pytest.fail("A finite analytics generation did not finish.")


def records(store, info, kind):
    return [
        value
        for (partition, _), value in store.container.items.items()
        if partition == store.partition(info.generation) and value["kind"] == kind
    ]


def amounts(store, info, grain):
    totals = {}
    for raw in records(store, info, grain):
        checkpoint = store.payload(store.decode(raw), RollupRecord).checkpoint
        for currency, amount in checkpoint.buckets[0].amounts_by_currency.items():
            totals[currency] = totals.get(currency, Decimal(0)) + amount
    return totals


def test_stale_worker_batch_cannot_write_rows_or_cursor_and_ready_generations_are_immutable(config):
    source = Source(1)
    store, data, worker, clock = setup(config, source)
    worker.step()
    head = store.payload(store.get("head"), AnalyticsHead)
    current = store.get("generation", head.active.info.generation)
    state = store.payload(current, GenerationState)
    changed = state.model_copy(update={"lease_owner": "new-owner"})
    store.replace(current, changed)
    row = store.make(
        "event",
        "e:" + "f" * 64,
        EventRecord(),
        generation=state.info.generation,
        expires_at=state.info.expires_at,
    )
    with pytest.raises(AnalyticsConflict):
        store.commit(current, state, [row])
    assert store.maybe_get(row.id, state.info.generation) is None
    clock.now += timedelta(minutes=3)
    complete = finish(AnalyticsMaterializer(store, data, clock=clock))
    ready = store.get("generation", complete.generation)
    with pytest.raises(ValueError, match="building generation"):
        store.commit(ready, store.payload(ready, GenerationState))


def test_generation_budget_never_publishes_partial_financial_data(config, monkeypatch):
    store, _, worker, _ = setup(config, Source(50))
    worker.step()
    monkeypatch.setattr("citadel_agent.analytics.snapshots.MAX_GENERATION_WRITE_BYTES", 1)
    assert worker.step() == "failed"
    assert store.payload(store.get("head"), AnalyticsHead).published is None


def test_source_ids_partitions_prompts_and_addresses_are_not_persisted(config):
    source = Source(1)
    source.documents = {
        "private-source-id": usage_document(
            id="private-source-id",
            requestBody="private-prompt-fixture",
            RequestIp="private-address-fixture",
            timestamp=(NOW - timedelta(hours=1)).isoformat(),
            _ts=int((NOW - timedelta(seconds=10)).timestamp()),
        )
    }
    store, _, worker, _ = setup(config, source)
    info = finish(worker)
    assert info.event_count == 1
    serialized = json.dumps(list(store.container.items.values()))
    assert "private-source-id" not in serialized
    assert "private-prompt-fixture" not in serialized
    assert "private-address-fixture" not in serialized
    assert "partitionKeyJson" not in serialized


def test_source_delta_replay_correction_and_full_delete_reconciliation_match_rebuild(config):
    source = Source(42)
    store, data, worker, clock = setup(config, source)
    first = finish(worker)
    before = records(store, first, "event")
    clock.now += timedelta(minutes=6)
    source.documents["0"]["promptTokens"] = 700
    source.documents["0"]["timestamp"] = (NOW - timedelta(days=2)).isoformat()
    source.documents["0"]["_ts"] = int(clock.now.timestamp())
    second = finish(AnalyticsMaterializer(store, data, clock=clock))
    assert second.event_count == 42
    assert records(store, first, "event") == before
    assert (
        sum(value["payload"]["observation"]["usage"]["prompt"] for value in records(store, second, "event"))
        == 4800
    )
    del source.documents["1"]
    clock.now += timedelta(minutes=6)
    third = finish(AnalyticsMaterializer(store, data, clock=clock), full=True)
    assert third.event_count == 41
    assert (
        sum(value["payload"]["observation"]["usage"]["prompt"] for value in records(store, third, "event"))
        == 4700
    )
    fresh_store, _, rebuilt, _ = setup(config, source, clock)
    fresh = finish(rebuilt)
    actual = [value["payload"]["observation"] for value in records(store, third, "event")]
    expected = [value["payload"]["observation"] for value in records(fresh_store, fresh, "event")]
    assert sorted(actual, key=lambda value: value["eventId"]) == sorted(
        expected, key=lambda value: value["eventId"]
    )


def test_committed_batch_with_lost_acknowledgement_resumes_after_lease_without_replaying_data(config):
    source = Source(61)
    store, data, worker, clock = setup(config, source)
    worker.step()
    store.container.lose_ack = True
    with pytest.raises(AzureError):
        worker.step()
    clock.now += timedelta(minutes=3)
    result = finish(AnalyticsMaterializer(store, data, clock=clock))
    assert result.event_count == 61
    assert (
        sum(value["payload"]["observation"]["usage"]["prompt"] for value in records(store, result, "event"))
        == 6100
    )


def test_invalid_source_update_removes_old_usage_and_quarantines_only_safe_metadata(config):
    source = Source(3)
    store, data, worker, clock = setup(config, source)
    finish(worker)
    clock.now += timedelta(minutes=6)
    source.documents["0"]["promptTokens"] = "private-malformed-value"
    source.documents["0"]["_ts"] = int(clock.now.timestamp())
    changed = finish(AnalyticsMaterializer(store, data, clock=clock))
    assert changed.event_count == 2
    assert changed.rejected_records == 1
    quarantine = records(store, changed, "quarantine")
    assert len(quarantine) == 1
    assert "private-malformed-value" not in json.dumps(quarantine)
    assert quarantine[0]["payload"]["observation"] is None


def test_hourly_checkpoints_merge_to_exact_daily_without_losing_missing_totals(config):
    source = Source(3)
    store, _data, worker, _ = setup(config, source)
    info = finish(worker)
    combined = IncrementalFinOps(config.scope, info.window, tuple(UsageDimension))
    for raw in records(store, info, "hour"):
        combined.merge(store.payload(store.decode(raw), RollupRecord).checkpoint)
    daily = [store.payload(store.decode(raw), RollupRecord).checkpoint for raw in records(store, info, "day")]
    assert combined.checkpoint() == daily[0]
    assert combined.checkpoint().buckets[0].usage_totals["prompt"] == 300


def percentage_price(window):
    return ModelPricingEntry(
        id="reviewed-fixed-period",
        model="gpt-test",
        deploymentName="test-deployment",
        isActive=True,
        CostUnit=1,
        CostPerInputUnit=1,
        BaseCost="1000",
        Currency="USD",
        CalculationMethod="percentage",
        effective_from=window.start,
        effective_to=window.end,
        AccountingPeriod=window,
    )


def test_ptu_stays_unpriced_until_exact_trusted_period_and_pricing_facts_match(config):
    source = Source(31)
    window = TimeRange(start=NOW - timedelta(days=1), end=NOW - timedelta(minutes=1))
    price = percentage_price(window)
    source.prices = [price.model_dump(by_alias=True, mode="json")]
    connected = config.model_copy(update={"completeness_container": "trusted-completeness"})
    store, data, worker, clock = setup(connected, source)
    incomplete = finish(worker)
    assert amounts(store, incomplete, "hour") == {}
    digest = "0" * 64
    for document in source.documents.values():
        event = data.normalize(document)
        observation = UsageObservation.model_validate(event.model_dump(exclude={"source_key"}))
        digest = add_digest(digest, observation)
    proof = PeriodCompletenessProof(
        scope=config.scope,
        source_fingerprint=source_fingerprint(connected),
        pricing_fingerprint=pricing_fingerprint([price]),
        window=window,
        complete_through=window.end,
        issued_at=NOW,
        source_write_cutoff=int(NOW.timestamp()),
        event_count=31,
        event_digest=digest,
    )
    source.proofs = [proof.model_dump(by_alias=True, mode="json")]
    clock.now += timedelta(minutes=6)
    complete = finish(AnalyticsMaterializer(store, data, clock=clock))
    assert amounts(store, complete, "hour") == {"USD": Decimal("1000")}
    assert amounts(store, complete, "day") == {"USD": Decimal("1000")}
    costs = [
        store.payload(store.decode(value), EventRecord).cost for value in records(store, complete, "event")
    ]
    assert all(value.status is PricingStatus.PRICED for value in costs)
    with localcontext() as context:
        context.prec = 100
        assert sum(value.amount for value in costs) == Decimal("1000")
    source.prices[0]["BaseCost"] = "2000"
    clock.now += timedelta(minutes=6)
    changed = finish(AnalyticsMaterializer(store, data, clock=clock))
    assert amounts(store, changed, "hour") == {}
