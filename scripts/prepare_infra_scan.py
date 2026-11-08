# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Adapt compiled ARM symbolic resource collections for Checkov's ARM reader.

Only resource collection containers change from dictionaries to arrays. Every
resource body, nested template, condition and copy expression is preserved.
The result is scanner input, not a deployable replacement for the Bicep output.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any


def scanner_template(compiled: dict[str, Any]) -> tuple[dict[str, Any], int]:
    result = copy.deepcopy(compiled)
    count = 0

    def visit(value: Any) -> None:
        nonlocal count
        if isinstance(value, dict):
            resources = value.get("resources")
            if (
                isinstance(resources, dict)
                and resources
                and all(
                    isinstance(item, dict) and {"type", "apiVersion", "name"}.issubset(item)
                    for item in resources.values()
                )
            ):
                value["resources"] = list(resources.values())
                count += len(resources)
            elif isinstance(resources, list) and all(
                isinstance(item, dict) and {"type", "apiVersion", "name"}.issubset(item) for item in resources
            ):
                count += len(resources)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    if count == 0:
        raise ValueError("compiled ARM template contains no resources to scan")
    return result, count


def verify_scan_result(data: dict[str, Any]) -> dict[str, Any]:
    summary = data.get("summary", data)
    if summary.get("resource_count", 0) < 1 or summary.get("passed", 0) < 1:
        raise ValueError("security scanner did not check any resources; zero-check success is not accepted")
    if summary.get("failed", 0) or summary.get("parsing_errors", 0):
        raise ValueError("security scanner reported failed checks or parsing errors")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--compiled", type=Path)
    source.add_argument("--result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.result is not None:
        data = json.loads(args.result.read_text(encoding="utf-8"))
        print("Infrastructure security scan:", verify_scan_result(data))
        return 0
    if args.output is None:
        parser.error("--output is required when preparing --compiled scanner input")
    result, count = scanner_template(json.loads(args.compiled.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared {count} resource declarations for ARM security scanning.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
