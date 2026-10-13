# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Template hashes must not depend on the Bicep compiler version."""

from __future__ import annotations

from pathlib import Path

from citadel_agent.azure_clients.bicep import CompiledDeployment


def _compiled(version: str, template_hash: str, nested_hash: str) -> CompiledDeployment:
    template = {
        "metadata": {"_generator": {"name": "bicep", "version": version, "templateHash": template_hash}},
        "resources": {
            "module": {
                "properties": {
                    "template": {
                        "metadata": {
                            "_generator": {"name": "bicep", "version": version, "templateHash": nested_hash}
                        },
                        "resources": [{"name": "x"}],
                    }
                }
            }
        },
    }
    return CompiledDeployment(Path("a.bicepparam"), Path("a.bicep"), template, {"p": {"value": 1}})


def test_template_hash_ignores_compiler_version() -> None:
    local = _compiled("0.42.1.51946", "111", "222")
    runner = _compiled("0.43.0.1", "333", "444")

    assert local.template_hash == runner.template_hash


def test_template_hash_still_detects_real_changes() -> None:
    original = _compiled("0.42.1.51946", "111", "222")
    changed = _compiled("0.42.1.51946", "111", "222")
    changed.template["resources"]["module"]["properties"]["template"]["resources"].append({"name": "y"})

    assert original.template_hash != changed.template_hash
