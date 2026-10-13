# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Agent tools exposed to the language model, grouped by capability.

``TOOL_NAMES`` is the single source of truth for the tool vocabulary. Skill playbooks refer to
tools by these names, and tests check that every name a playbook mentions exists here and is
implemented by the agent.
"""

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "gateway_overview",
        "list_models",
        "search_docs",
        "read_doc",
        "list_access_contracts",
        "describe_access_contract",
        "generate_client_code",
        "explain_error",
        "gateway_logs",
        "usage_report",
        "smoke_test",
        "preflight",
        "backend_contract_summary",
        "change_status",
    }
)

CHANGE_TOOLS: frozenset[str] = frozenset(
    {
        "draft_access_contract",
        "draft_backend_change",
        "draft_publish_contract",
        "draft_alert_rules",
        "start_key_rotation",
        "advance_key_rotation",
        "prepare_change",
        "execute_change",
    }
)

TOOL_NAMES: frozenset[str] = READ_ONLY_TOOLS | CHANGE_TOOLS
