# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Citadel gateway data-plane client: version endpoints, model discovery, smoke tests and response parsing."""

from citadel_agent.gateway.client import (
    GatewayClient,
    GatewayRequestError,
    parse_deployments,
    parse_error,
    response_info,
)
from citadel_agent.gateway.models import (
    BackendContractView,
    GatewayError,
    GatewayResponseInfo,
    ModelInfo,
    SmokeTestResult,
)

__all__ = [
    "BackendContractView",
    "GatewayClient",
    "GatewayError",
    "GatewayRequestError",
    "GatewayResponseInfo",
    "ModelInfo",
    "SmokeTestResult",
    "parse_deployments",
    "parse_error",
    "response_info",
]
