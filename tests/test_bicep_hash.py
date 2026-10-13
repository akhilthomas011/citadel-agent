# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Template identity must come from Bicep sources, never from compiler output."""

from __future__ import annotations

from pathlib import Path

import pytest

from citadel_agent.azure_clients.bicep import CompiledDeployment, source_files

UPSTREAM = Path(__file__).resolve().parents[1] / "upstream" / "ai-hub-gateway"
ACCESS_TEMPLATE = UPSTREAM / "bicep" / "infra" / "citadel-access-contracts" / "main.bicep"


def _compiled(template_file: Path, *, version: str = "0.42.1", extra: str = "") -> CompiledDeployment:
    template = {
        "metadata": {"_generator": {"name": "bicep", "version": version, "templateHash": "1"}},
        "resources": {"x": extra},
    }
    return CompiledDeployment(Path("a.bicepparam"), template_file, template, {"p": {"value": 1}})


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "child.bicep").write_text("param a string\n", encoding="utf-8", newline="\n")
    (tmp_path / "policy.xml").write_text("<policies />\n", encoding="utf-8", newline="\n")
    main = tmp_path / "main.bicep"
    main.write_text(
        "module child 'modules/child.bicep' = { name: 'c' }\n"
        "var xml = loadTextContent('policy.xml')\n"
        "module remote 'br/public:avm/res/x/y:1.0.0' = { name: 'r' }\n",
        encoding="utf-8",
        newline="\n",
    )
    return main


def test_hash_ignores_the_compiler_output(sources: Path) -> None:
    older = _compiled(sources, version="0.42.1", extra="a")
    newer = _compiled(sources, version="0.47.16", extra="completely different codegen")

    assert older.template_hash == newer.template_hash


def test_hash_ignores_line_endings(sources: Path, tmp_path: Path) -> None:
    lf = _compiled(sources).template_hash
    child = tmp_path / "modules" / "child.bicep"
    child.write_bytes(child.read_bytes().replace(b"\n", b"\r\n"))

    assert _compiled(sources).template_hash == lf


def test_hash_follows_modules_and_loaded_files(sources: Path, tmp_path: Path) -> None:
    assert set(source_files(sources)) == {"main.bicep", "modules/child.bicep", "policy.xml"}
    before = _compiled(sources).template_hash

    (tmp_path / "modules" / "child.bicep").write_text("param a int\n", encoding="utf-8", newline="\n")
    assert _compiled(sources).template_hash != before


def test_hash_falls_back_to_the_compiled_template_without_sources() -> None:
    missing = Path("does-not-exist.bicep")
    one = _compiled(missing, version="1", extra="a")
    two = _compiled(missing, version="2", extra="a")
    changed = _compiled(missing, extra="b")

    assert one.template_hash == two.template_hash
    assert one.template_hash != changed.template_hash


@pytest.mark.skipif(not ACCESS_TEMPLATE.is_file(), reason="upstream submodule is not checked out")
def test_upstream_access_template_modules_are_discovered() -> None:
    files = source_files(ACCESS_TEMPLATE)

    assert "main.bicep" in files
    assert len(files) > 1, "the access template's modules were not followed"
