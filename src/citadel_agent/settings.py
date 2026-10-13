# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Runtime settings (environment variables with the ``CITADEL_AGENT_`` prefix, or a ``.env`` file)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Where the canonical contracts repository is and how the agent connects."""

    model_config = SettingsConfigDict(env_prefix="CITADEL_AGENT_", env_file=".env", extra="ignore")

    repo_url: str | None = Field(
        default=None,
        description=(
            "Canonical contracts repository: a hosted git URL (https://github.com/org/repo.git) "
            "or a local path for local-only development"
        ),
    )
    workspace_dir: Path | None = Field(
        default=None, description="The agent's own working clone (default: <state dir>/repo)"
    )
    base_branch: str = "main"
    bootstrap_policy: Path | None = Field(
        default=None, description="Local admin policy for dev bootstrap only (dev-class environments)"
    )
    environment: str | None = Field(
        default=None, description="Environment to work with (defaults to the policy's)"
    )
    model: str | None = Field(default=None, description="Override the model the agent uses")
    gateway_key: str | None = Field(
        default=None, description="Agent gateway key for local development only (prefer Key Vault)"
    )
    state_dir: Path | None = Field(default=None, description="Runtime state directory")
    otel_endpoint: str | None = Field(
        default=None, description="OTLP endpoint for OpenTelemetry export (opt-in)"
    )
    otel_console: bool = Field(default=False, description="Print OpenTelemetry spans to the console")
