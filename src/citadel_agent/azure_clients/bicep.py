# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Compile Bicep templates and ``.bicepparam`` files with the Bicep CLI.

Compilation writes to files (``--outfile``) rather than standard output: on Windows the Azure CLI
cannot reliably pipe non-ASCII template content, and the upstream policies contain such
characters.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from citadel_agent.proc import CommandError, run_command

_USING = re.compile(r"^\s*using\s+'(?P<path>[^']+)'", re.MULTILINE)
_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<column>\d+)\)\s*:\s*(?P<level>Error|Warning|Info)\s+"
    r"(?P<code>[A-Za-z0-9-]+):\s*(?P<message>.*)$"
)
COMPILE_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class BicepDiagnostic:
    """One diagnostic reported by the Bicep compiler."""

    file: str
    line: int
    column: int
    level: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{Path(self.file).name}({self.line},{self.column}) {self.level} {self.code}: {self.message}"


class BicepCompileError(RuntimeError):
    """Raised when Bicep compilation fails."""

    def __init__(self, message: str, diagnostics: tuple[BicepDiagnostic, ...] = ()) -> None:
        details = "; ".join(str(d) for d in diagnostics if d.level == "Error")
        super().__init__(f"{message}{': ' + details if details else ''}")
        self.diagnostics = diagnostics


def canonical_hash(data: Any) -> str:
    """Return ``sha256:<hex>`` of the canonical JSON encoding of ``data``."""
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def file_hash(path: Path) -> str:
    """Return ``sha256:<hex>`` of a file's bytes."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _without_generator(node: Any) -> Any:
    """Drop Bicep's ``_generator`` metadata (compiler version and its own hash) from a template."""
    if isinstance(node, dict):
        return {key: _without_generator(value) for key, value in node.items() if key != "_generator"}
    if isinstance(node, list):
        return [_without_generator(item) for item in node]
    return node


@dataclass(frozen=True)
class CompiledDeployment:
    """A compiled ARM template with its parameter values, ready for what-if or deployment."""

    params_file: Path
    template_file: Path
    template: dict[str, Any]
    parameters: dict[str, Any]
    diagnostics: tuple[BicepDiagnostic, ...] = field(default=())

    @property
    def template_hash(self) -> str:
        # The compiler version must not change the hash, or a change prepared with one Bicep
        # version could never be deployed from a pipeline that has another.
        return canonical_hash(_without_generator(self.template))

    @property
    def parameters_hash(self) -> str:
        return canonical_hash(self.parameters)

    @property
    def warnings(self) -> list[BicepDiagnostic]:
        return [d for d in self.diagnostics if d.level == "Warning"]


def parse_diagnostics(output: str) -> tuple[BicepDiagnostic, ...]:
    """Parse Bicep compiler diagnostics from its error output."""
    diagnostics: list[BicepDiagnostic] = []
    for line in output.splitlines():
        match = _DIAGNOSTIC.match(line.strip())
        if match:
            diagnostics.append(
                BicepDiagnostic(
                    file=match["file"],
                    line=int(match["line"]),
                    column=int(match["column"]),
                    level=match["level"],
                    code=match["code"],
                    message=match["message"].strip(),
                )
            )
    return tuple(diagnostics)


def resolve_using(params_file: Path) -> Path:
    """Return the template referenced by a ``.bicepparam`` file's ``using`` statement."""
    text = params_file.read_text(encoding="utf-8")
    match = _USING.search(text)
    if not match:
        raise BicepCompileError(f"{params_file.name} has no 'using' statement.")
    return (params_file.parent / match["path"]).resolve()


def _run_bicep(command: str, source: Path) -> tuple[dict[str, Any], tuple[BicepDiagnostic, ...]]:
    with tempfile.TemporaryDirectory(prefix="citadel-bicep-") as tmp:
        out_file = Path(tmp) / "compiled.json"
        try:
            result = run_command(
                "bicep",
                [command, str(source), "--outfile", str(out_file)],
                cwd=source.parent,
                timeout=COMPILE_TIMEOUT_SECONDS,
                check=False,
            )
        except CommandError as exc:
            raise BicepCompileError(f"Could not run the Bicep CLI: {exc}") from exc
        diagnostics = parse_diagnostics(result.stderr)
        if result.returncode != 0 or not out_file.is_file():
            raise BicepCompileError(f"Bicep {command} failed for {source.name}", diagnostics)
        return json.loads(out_file.read_text(encoding="utf-8")), diagnostics


def compile_template(template_file: Path) -> tuple[dict[str, Any], tuple[BicepDiagnostic, ...]]:
    """Compile a ``.bicep`` template to an ARM JSON template."""
    return _run_bicep("build", template_file)


def compile_params(params_file: Path) -> tuple[dict[str, Any], tuple[BicepDiagnostic, ...]]:
    """Compile a ``.bicepparam`` file and return its ARM ``parameters`` object."""
    document, diagnostics = _run_bicep("build-params", params_file)
    return document.get("parameters", {}), diagnostics


def compile_deployment(params_file: Path) -> CompiledDeployment:
    """Compile a ``.bicepparam`` file together with the template it references."""
    params_path = params_file.resolve()
    template_path = resolve_using(params_path)
    template, template_diagnostics = compile_template(template_path)
    parameters, param_diagnostics = compile_params(params_path)
    return CompiledDeployment(
        params_file=params_path,
        template_file=template_path,
        template=template,
        parameters=parameters,
        diagnostics=template_diagnostics + param_diagnostics,
    )
