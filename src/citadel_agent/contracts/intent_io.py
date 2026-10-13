# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Load and save contract intents (``contract.yaml``) and locate them in the canonical repository."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import yaml
from pydantic import Field, TypeAdapter

from citadel_agent.contracts.access import AccessContractIntent
from citadel_agent.contracts.alerts import AlertsIntent
from citadel_agent.contracts.backend import BackendContractIntent
from citadel_agent.contracts.publish import PublishContractIntent

INTENT_FILE_NAME = "contract.yaml"
CONTRACTS_ROOT = PurePosixPath("contracts")

ContractIntent = Annotated[
    AccessContractIntent | BackendContractIntent | PublishContractIntent | AlertsIntent,
    Field(discriminator="kind"),
]
_INTENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(ContractIntent)


def parse_intent(data: Any) -> ContractIntent:
    """Validate raw data (for example parsed YAML) as a contract intent."""
    return _INTENT_ADAPTER.validate_python(data)


def load_intent(path: Path) -> ContractIntent:
    """Load and validate a ``contract.yaml`` file."""
    return parse_intent(yaml.safe_load(path.read_text(encoding="utf-8")))


def dump_intent(intent: ContractIntent) -> str:
    """Serialize an intent to YAML with camelCase keys and a stable field order."""
    data = intent.model_dump(by_alias=True, exclude_none=True, mode="json")
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def save_intent(intent: ContractIntent, path: Path) -> Path:
    """Write an intent to ``path`` (creating parent folders) and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_intent(intent), encoding="utf-8", newline="\n")
    return path


def _segment(value: str) -> str:
    return value.strip().lower()


def intent_directory(intent: ContractIntent) -> PurePosixPath:
    """Return the folder (relative to the repository root) that holds an intent and its generated files."""
    env = _segment(intent.environment)
    if isinstance(intent, AccessContractIntent):
        uc = intent.use_case
        return CONTRACTS_ROOT / "access" / _segment(uc.business_unit) / _segment(uc.use_case_name) / env
    if isinstance(intent, BackendContractIntent):
        return CONTRACTS_ROOT / "backend" / env
    if isinstance(intent, PublishContractIntent):
        return CONTRACTS_ROOT / "publish" / _segment(intent.name) / env
    return CONTRACTS_ROOT / "alerts" / _segment(intent.scope_name) / env


def intent_path(intent: ContractIntent) -> PurePosixPath:
    """Return the repository-relative path of an intent's ``contract.yaml``."""
    return intent_directory(intent) / INTENT_FILE_NAME
