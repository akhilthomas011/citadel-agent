# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Serialize Python values as Bicep literals and write ``.bicepparam`` files."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INDENT = "  "


class BicepExpression(str):
    """A raw Bicep expression emitted verbatim, for example ``loadTextContent('policy.xml')``."""

    __slots__ = ()


def load_text_content(file_name: str) -> BicepExpression:
    """Return a ``loadTextContent('<file>')`` expression for a file next to the parameter file."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", file_name):
        raise ValueError(f"Unsupported file name for loadTextContent: {file_name!r}")
    return BicepExpression(f"loadTextContent('{file_name}')")


def bicep_string(value: str) -> str:
    """Return a single-quoted Bicep string literal with all special characters escaped."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("${", "\\${")
    )
    return f"'{escaped}'"


def _key(name: str) -> str:
    return name if _IDENTIFIER.match(name) else bicep_string(name)


def to_bicep(value: Any, level: int = 0) -> str:
    """Render a JSON-compatible value (plus :class:`BicepExpression`) as a Bicep literal."""
    pad = _INDENT * level
    inner = _INDENT * (level + 1)
    if isinstance(value, BicepExpression):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Bicep cannot represent non-finite numbers")
        # Bicep has integer literals only; decimals go through json().
        return str(int(value)) if value.is_integer() else f"json('{value!r}')"
    if isinstance(value, str):
        return bicep_string(value)
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        lines = [f"{inner}{_key(str(k))}: {to_bicep(v, level + 1)}" for k, v in value.items()]
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"
    if isinstance(value, list | tuple):
        if not value:
            return "[]"
        lines = [f"{inner}{to_bicep(item, level + 1)}" for item in value]
        return "[\n" + "\n".join(lines) + f"\n{pad}]"
    raise TypeError(f"Cannot render {type(value).__name__} as Bicep")


def render_param_file(using: str, parameters: Mapping[str, Any], header: Iterable[str] = ()) -> str:
    """Render a complete ``.bicepparam`` document."""
    lines = [f"// {line}" if line else "//" for line in header]
    if lines:
        lines.append("")
    lines.append(f"using {bicep_string(using)}")
    for name, value in parameters.items():
        if not _IDENTIFIER.match(name):
            raise ValueError(f"Invalid parameter name {name!r}")
        lines.append("")
        lines.append(f"param {name} = {to_bicep(value)}")
    return "\n".join(lines) + "\n"
