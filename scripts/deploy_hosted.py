# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Compatibility entry point for deploying to prepared private hosts."""

import sys

from citadel_agent.portal.delivery import az_json, check_health, deploy, main, validate_hosts

__all__ = ["az_json", "check_health", "deploy", "main", "validate_hosts"]


if __name__ == "__main__":
    sys.exit(main())
