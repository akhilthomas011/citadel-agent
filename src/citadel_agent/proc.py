# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Allowlisted subprocess execution.

Every external command the agent runs (git, bicep, az, azd) goes through :func:`run_command`.
Commands are passed as argument lists (never through a shell), executables must be on an
allowlist, and arguments destined for Windows batch files (for example ``az.cmd``) are rejected
when they contain characters that ``cmd.exe`` would interpret.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ALLOWED_EXECUTABLES: frozenset[str] = frozenset({"git", "bicep", "az", "azd", "gh"})
DEFAULT_TIMEOUT_SECONDS = 600

# Characters that cmd.exe interprets even inside quoted arguments.
_BATCH_UNSAFE_CHARS = frozenset('&|<>^%!"\r\n')


class CommandError(RuntimeError):
    """Raised when a command cannot be started or exits with a non-zero status."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class CommandNotAllowedError(CommandError):
    """Raised when a command or one of its arguments is rejected before execution."""


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a completed command."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _fallback_location(name: str) -> str | None:
    # The Azure CLI installs Bicep to ~/.azure/bin without adding it to PATH.
    if name == "bicep":
        candidate = Path.home() / ".azure" / "bin" / ("bicep.exe" if sys.platform == "win32" else "bicep")
        if candidate.is_file():
            return str(candidate)
    return None


def resolve_executable(name: str) -> str:
    """Resolve an allowlisted executable name to an absolute path.

    Args:
        name: Bare executable name such as ``git`` or ``az``.

    Returns:
        Absolute path of the executable.

    Raises:
        CommandNotAllowedError: If the name is not allowlisted or contains a path.
        CommandError: If the executable is not installed.
    """
    if name not in ALLOWED_EXECUTABLES:
        raise CommandNotAllowedError(f"Executable '{name}' is not on the allowlist.")
    resolved = shutil.which(name) or _fallback_location(name)
    if resolved is None:
        raise CommandError(f"Executable '{name}' was not found on PATH.")
    return resolved


def _is_batch_file(path: str) -> bool:
    return sys.platform == "win32" and Path(path).suffix.lower() in {".cmd", ".bat"}


def _check_batch_arguments(args: Sequence[str]) -> None:
    for arg in args:
        unsafe = sorted(set(arg) & _BATCH_UNSAFE_CHARS)
        if unsafe:
            raise CommandNotAllowedError(
                "Argument rejected: it contains characters that are unsafe for a Windows batch "
                f"file invocation ({''.join(unsafe)!r})."
            )


def run_command(
    executable: str,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    check: bool = True,
    redact: Callable[[str], str] | None = None,
) -> CommandResult:
    """Run an allowlisted executable with an argument list.

    Args:
        executable: Bare executable name from :data:`ALLOWED_EXECUTABLES`.
        args: Arguments passed to the executable.
        cwd: Working directory for the command.
        extra_env: Environment variables added to the current environment.
        timeout: Seconds before the command is terminated.
        check: Raise :class:`CommandError` on a non-zero exit status.
        redact: Optional function applied to command lines and output before they are logged.

    Returns:
        The completed command result.

    Raises:
        CommandNotAllowedError: If the command or an argument is rejected.
        CommandError: If the command fails to start, times out or (with ``check``) fails.
    """
    exe_path = resolve_executable(executable)
    arg_list = [str(a) for a in args]
    if _is_batch_file(exe_path):
        _check_batch_arguments(arg_list)

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    scrub = redact or (lambda text: text)
    logger.debug("Running: %s %s", executable, scrub(" ".join(arg_list)))
    try:
        completed = subprocess.run(  # noqa: S603 - argument list, allowlisted executable, no shell
            [exe_path, *arg_list],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"Executable '{executable}' could not be started.") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"Command '{executable}' timed out after {timeout} seconds.") from exc

    result = CommandResult(
        args=(executable, *arg_list),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and result.returncode != 0:
        raise CommandError(
            f"Command '{executable}' failed with exit code {result.returncode}: "
            f"{scrub(result.stderr.strip())[:2000]}",
            returncode=result.returncode,
            stderr=scrub(result.stderr),
        )
    return result
