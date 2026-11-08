# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Container entry point for the hosted BFF or private agent service."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

import uvicorn

from citadel_agent.portal.runtime import (
    PortalRuntimeSettings,
    create_agent_app_from_settings,
    create_bff_app_from_settings,
)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("bff", "agent", "analytics", "assistant-worker"))
    parser.add_argument(
        "--force-full", action="store_true", help="Reconcile analytics from the entire source."
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entry point
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.service == "analytics":
        from citadel_agent.analytics.worker import run_worker

        return run_worker(force_full=args.force_full)
    if args.force_full:
        raise ValueError("--force-full is supported only by the analytics worker.")
    if args.service == "assistant-worker":
        import asyncio

        from citadel_agent.portal.worker import run_assistant_worker

        return asyncio.run(run_assistant_worker())
    settings = PortalRuntimeSettings()
    if args.service == "agent":
        if settings.hub_config is None or settings.bff_service_client_id is None:
            raise ValueError("The hosted agent requires HUB_CONFIG and the BFF managed-identity client ID.")
        app = create_agent_app_from_settings(settings)
    else:
        app = create_bff_app_from_settings(settings)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=False,
        access_log=False,
        limit_concurrency=64,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=330,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
