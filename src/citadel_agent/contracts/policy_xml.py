# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Structural validation of APIM policy documents.

APIM policies are XML whose attribute values and text may contain C# policy expressions
(``@(...)`` and ``@{...}``). Upstream Citadel policies use APIM's lenient ``rawxml`` syntax, where
expressions contain unescaped quotes and angle brackets, so they are not strictly valid XML. The
linter masks expressions (honoring C# string literals, including verbatim ``@"..."`` strings)
before parsing the remaining structure with a hardened XML parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

SECTIONS = ("inbound", "backend", "outbound", "on-error")
_PLACEHOLDER = "__APIM_EXPR_{}__"


class LintLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class LintIssue:
    level: LintLevel
    message: str


class ExpressionScanError(ValueError):
    """Raised when a policy expression is not terminated."""


def _skip_string(text: str, index: int) -> int:
    """Return the index just after the C# string or char literal starting at ``index``."""
    if text.startswith('@"', index) or text.startswith('$@"', index) or text.startswith('@$"', index):
        start = text.index('"', index) + 1
        position = start
        while position < len(text):
            if text[position] == '"':
                if position + 1 < len(text) and text[position + 1] == '"':
                    position += 2
                    continue
                return position + 1
            position += 1
        raise ExpressionScanError("Unterminated verbatim string literal in policy expression")
    quote = text[index] if text[index] in "\"'" else text[index + 1]
    position = text.index(quote, index) + 1
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == quote:
            return position + 1
        position += 1
    raise ExpressionScanError("Unterminated string literal in policy expression")


def _expression_end(text: str, start: int) -> int:
    """Return the index just after the expression that starts at ``start`` (pointing at '@')."""
    opener = text[start + 1]
    closer = ")" if opener == "(" else "}"
    depth = 0
    position = start + 1
    while position < len(text):
        char = text[position]
        if char in "\"'" or (char in "@$" and text[position + 1 : position + 2] in ('"', "@", "$")):
            if char in "@$" and not text.startswith(('@"', '$@"', '@$"', '$"'), position):
                position += 1
                continue
            position = _skip_string(text, position)
            continue
        # C# comments may contain apostrophes ("don't") that must not start a char literal.
        if text.startswith("//", position):
            newline = text.find("\n", position)
            position = len(text) if newline == -1 else newline
            continue
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end == -1:
                raise ExpressionScanError("Unterminated comment in policy expression")
            position = end + 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    raise ExpressionScanError(f"Unterminated policy expression starting at offset {start}")


def mask_expressions(text: str) -> tuple[str, list[str]]:
    """Replace every ``@(...)`` / ``@{...}`` expression with a placeholder.

    Returns:
        The masked text and the list of extracted expressions (placeholder index = list index).
    """
    pieces: list[str] = []
    expressions: list[str] = []
    position = 0
    pattern = re.compile(r"@[({]")
    while True:
        match = pattern.search(text, position)
        if not match:
            pieces.append(text[position:])
            break
        start = match.start()
        end = _expression_end(text, start)
        pieces.append(text[position:start])
        pieces.append(_PLACEHOLDER.format(len(expressions)))
        expressions.append(text[start:end])
        position = end
    return "".join(pieces), expressions


def lint_policy(text: str) -> list[LintIssue]:
    """Validate the structure of an APIM policy document.

    Checks: expressions terminate, the document parses, the root is ``<policies>``, sections are
    known and unique, each section inherits the outer scope with ``<base />`` and
    ``allowedModels`` contains no whitespace. ``enableResponseHeaders`` is reported as a warning.
    """
    issues: list[LintIssue] = []
    try:
        masked, _ = mask_expressions(text)
    except ExpressionScanError as exc:
        return [LintIssue(LintLevel.ERROR, str(exc))]
    try:
        root = ElementTree.fromstring(masked)
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        return [LintIssue(LintLevel.ERROR, f"Policy is not well-formed: {exc}")]

    if root.tag != "policies":
        issues.append(LintIssue(LintLevel.ERROR, f"Root element must be <policies>, found <{root.tag}>"))
        return issues

    seen: list[str] = []
    for section in root:
        if section.tag not in SECTIONS:
            issues.append(LintIssue(LintLevel.ERROR, f"Unknown policy section <{section.tag}>"))
            continue
        if section.tag in seen:
            issues.append(LintIssue(LintLevel.ERROR, f"Section <{section.tag}> appears more than once"))
        seen.append(section.tag)
        if section.find("base") is None:
            issues.append(LintIssue(LintLevel.WARNING, f"Section <{section.tag}> does not include <base />"))

    for element in root.iter("set-variable"):
        name = element.get("name", "")
        value = element.get("value", "")
        if name == "allowedModels" and re.search(r"\s", value):
            issues.append(
                LintIssue(LintLevel.ERROR, "allowedModels must be a comma-separated list without spaces")
            )
        if name == "enableResponseHeaders" and value.strip().lower() not in {"false", "@(false)"}:
            issues.append(
                LintIssue(
                    LintLevel.WARNING,
                    "enableResponseHeaders exposes UAIG-* debug headers; avoid in production",
                )
            )
    return issues


def has_errors(issues: list[LintIssue]) -> bool:
    return any(issue.level is LintLevel.ERROR for issue in issues)
