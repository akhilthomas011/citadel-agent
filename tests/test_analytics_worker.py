"""Scheduled analytics need no session, model credential or permission to write source data."""

import itertools
from types import SimpleNamespace

import pytest

from citadel_agent.analytics.snapshots import AnalyticsHead, AnalyticsStore
from citadel_agent.analytics.worker import AnalyticsWorkerSettings, run_worker
from citadel_agent.portal.server import create_parser, main
from test_analytics_snapshots import AnalyticsContainer, Source
from test_portal_product import config as config_fixture

config = config_fixture


class Cosmos:
    def __init__(self, container):
        self.container = container
        self.requested = []

    def get_database_client(self, name):
        self.requested.append(name)
        return self

    def get_container_client(self, name):
        self.requested.append(name)
        return self.container


def settings(config, **changes):
    return AnalyticsWorkerSettings(
        _env_file=None,
        cosmos_endpoint="https://state.documents.azure.com",
        cosmos_database="citadel-portal",
        analytics_container="portal-analytics",
        hub_config=config,
        **changes,
    )


def test_finite_worker_publishes_using_only_analytics_settings_and_injected_data_clients(config):
    container = AnalyticsContainer()
    cosmos = Cosmos(container)
    assert (
        run_worker(
            settings(config),
            credential=SimpleNamespace(),
            cosmos_client=cosmos,
            usage_reader=Source(41),
        )
        == 0
    )
    store = AnalyticsStore(container, config.scope)
    assert store.payload(store.get("head"), AnalyticsHead).published.event_count == 41
    assert cosmos.requested == ["citadel-portal", "portal-analytics"]


def test_worker_yields_at_a_durable_checkpoint_and_the_next_process_finishes(config, caplog):
    container = AnalyticsContainer()
    cosmos = Cosmos(container)
    source = Source(61)
    budget = itertools.count(0, 0.6)
    assert (
        run_worker(
            settings(config, analytics_max_work_seconds=1),
            credential=SimpleNamespace(),
            cosmos_client=cosmos,
            usage_reader=source,
            monotonic=lambda: next(budget),
        )
        == 0
    )
    store = AnalyticsStore(container, config.scope)
    assert store.payload(store.get("head"), AnalyticsHead).published is None
    assert store.payload(store.get("head"), AnalyticsHead).active is not None
    assert (
        run_worker(
            settings(config),
            credential=SimpleNamespace(),
            cosmos_client=cosmos,
            usage_reader=source,
        )
        == 0
    )
    assert store.payload(store.get("head"), AnalyticsHead).published.event_count == 61


def test_worker_rejects_embedded_credentials_and_invalid_managed_identity(config):
    with pytest.raises(ValueError):
        settings(config, managed_identity_client_id="not-a-guid")
    values = settings(config).model_dump()
    values["cosmos_endpoint"] = "https://user:must-not-be-used@state.documents.azure.com"
    with pytest.raises(ValueError):
        AnalyticsWorkerSettings(_env_file=None, **values)


def test_analytics_entrypoint_has_no_http_server_and_full_rebuild_is_not_a_bff_option():
    assert create_parser().parse_args(["analytics", "--force-full"]).service == "analytics"
    with pytest.raises(ValueError, match="only by the analytics"):
        main(["bff", "--force-full"])
