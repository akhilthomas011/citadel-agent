# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Citadel Agent: an AI assistant for using and operating the Citadel Governance Hub.

The agent helps application teams and platform operators work with the Citadel Governance Hub
(an Azure API Management based AI gateway) through its contract-driven Bicep framework.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    __version__ = _package_version("citadel-agent")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0+local"


def main() -> None:
    """Console-script entry point."""
    from citadel_agent.cli import main as cli_main

    cli_main()
