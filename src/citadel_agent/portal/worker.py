# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Finite private worker for durably accepted assistant turns; no HTTP listener or effect replay."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from uuid import uuid4

from azure.core.exceptions import AzureError
from starlette.concurrency import run_in_threadpool

from citadel_agent.portal.runtime import PortalRuntimeSettings, create_agent_app_from_settings
from citadel_agent.portal.sessions import PortalStateConflict

logger = logging.getLogger(__name__)


async def run_assistant_worker(settings: PortalRuntimeSettings | None = None) -> int:
    settings = settings or PortalRuntimeSettings()
    if not settings.durable_turns_enabled:
        raise ValueError("The assistant worker requires durable turns to be enabled.")
    app = create_agent_app_from_settings(settings)
    product = app.state.portal_product
    if product is None:
        raise ValueError("The assistant worker requires the configured product and hub scope.")
    worker_id = uuid4().hex
    async with app.router.lifespan_context(app):
        started = time.monotonic()
        acquired = False
        code = 0
        try:
            acquired = await run_in_threadpool(product.assistant.claim_worker, worker_id)
            if acquired:
                stop = threading.Event()

                def heartbeat():
                    while not stop.wait(30):
                        product.assistant.renew_worker(worker_id)

                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(asyncio.to_thread(heartbeat))
                    try:
                        while time.monotonic() - started < settings.assistant_worker_max_seconds:
                            if not await product.assistant.process_queued_turn(worker_id):
                                break
                    finally:
                        stop.set()
        except* (AzureError, PortalStateConflict) as errors:
            logger.error(
                "Durable assistant worker lost its dependency/ownership (%s).", type(errors).__name__
            )
            code = 1
        finally:
            if acquired:
                try:
                    await run_in_threadpool(product.assistant.renew_worker, worker_id, release=True)
                except (AzureError, PortalStateConflict) as exc:
                    logger.error("Assistant worker lease release failed (%s).", type(exc).__name__)
                    code = 1
        return code
