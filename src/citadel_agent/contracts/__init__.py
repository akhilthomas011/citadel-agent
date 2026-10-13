# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Contract intents and models, product policy builder and renderers."""

from citadel_agent.contracts.access import (
    AccessContractIntent,
    AlertToggles,
    AssetEndpoint,
    CallLimit,
    ContentSafetyPolicy,
    FoundryConnectionIntent,
    JwtPolicy,
    KeyRotationState,
    ModelTokenLimit,
    PiiPolicy,
    ProductPolicyIntent,
    ResiliencyIntent,
    ServiceIntent,
    TokenLimit,
    UsageDimension,
    UseCase,
)
from citadel_agent.contracts.alerts import AlertRule, AlertsIntent, EmailReceiver, default_alert_rules
from citadel_agent.contracts.backend import BackendContractIntent, BackendEntry, ModelAlias, ModelEntry
from citadel_agent.contracts.common import AzureResourceRef, ContractMetadata, FoundryProjectRef
from citadel_agent.contracts.intent_io import (
    INTENT_FILE_NAME,
    ContractIntent,
    dump_intent,
    intent_directory,
    intent_path,
    load_intent,
    parse_intent,
    save_intent,
)
from citadel_agent.contracts.publish import PublishAsset, PublishBackend, PublishContractIntent

__all__ = [
    "INTENT_FILE_NAME",
    "AccessContractIntent",
    "AlertRule",
    "AlertToggles",
    "AlertsIntent",
    "AssetEndpoint",
    "AzureResourceRef",
    "BackendContractIntent",
    "BackendEntry",
    "CallLimit",
    "ContentSafetyPolicy",
    "ContractIntent",
    "ContractMetadata",
    "EmailReceiver",
    "FoundryConnectionIntent",
    "FoundryProjectRef",
    "JwtPolicy",
    "KeyRotationState",
    "ModelAlias",
    "ModelEntry",
    "ModelTokenLimit",
    "PiiPolicy",
    "ProductPolicyIntent",
    "PublishAsset",
    "PublishBackend",
    "PublishContractIntent",
    "ResiliencyIntent",
    "ServiceIntent",
    "TokenLimit",
    "UsageDimension",
    "UseCase",
    "default_alert_rules",
    "dump_intent",
    "intent_directory",
    "intent_path",
    "load_intent",
    "parse_intent",
    "save_intent",
]
