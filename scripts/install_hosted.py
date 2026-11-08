# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Preview-first entry point for the customer private hosting installer."""

import sys

from citadel_agent.portal.installation import main

if __name__ == "__main__":
    sys.exit(main())
