# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Read-only release evidence check. Nonzero means do not publish/deploy."""

import sys

from citadel_agent.portal.release import main

if __name__ == "__main__":
    sys.exit(main())
