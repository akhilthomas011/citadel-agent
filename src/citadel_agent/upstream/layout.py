# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Locations of templates, policies and documentation inside the pinned upstream checkout.

The agent never copies upstream infrastructure code. Everything it deploys or reads is resolved
through :class:`UpstreamLayout`, so moving to a new upstream commit only requires updating the
submodule (and passing the drift tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from citadel_agent.paths import find_project_root
from citadel_agent.upstream.lock import RELEASE_FILE_NAME, read_lock

_DOC_EXCLUDED_PARTS = frozenset({".git", "node_modules", ".venv", "assets"})


@dataclass(frozen=True)
class UpstreamLayout:
    """Resolved paths inside an upstream Citadel checkout."""

    root: Path

    @property
    def infra(self) -> Path:
        return self.root / "bicep" / "infra"

    @property
    def release_file(self) -> Path:
        return self.root / RELEASE_FILE_NAME

    # Access contracts -------------------------------------------------------------------------
    @property
    def access_contracts_dir(self) -> Path:
        return self.infra / "citadel-access-contracts"

    @property
    def access_contract_template(self) -> Path:
        return self.access_contracts_dir / "main.bicep"

    @property
    def access_base_contract_dir(self) -> Path:
        return self.access_contracts_dir / "base-contracts" / "common"

    @property
    def access_default_policy(self) -> Path:
        return self.access_contracts_dir / "policies" / "default-ai-product-policy.xml"

    @property
    def access_default_multi_policy(self) -> Path:
        return self.access_contracts_dir / "policies" / "default-multi-product-policy.xml"

    # Backend contracts ------------------------------------------------------------------------
    @property
    def backend_onboarding_dir(self) -> Path:
        return self.infra / "llm-backend-onboarding"

    @property
    def backend_contract_template(self) -> Path:
        return self.backend_onboarding_dir / "main.bicep"

    # Publish contracts ------------------------------------------------------------------------
    @property
    def publish_contracts_dir(self) -> Path:
        return self.infra / "citadel-publish-contracts"

    @property
    def publish_contract_template(self) -> Path:
        return self.publish_contracts_dir / "main.bicep"

    @property
    def publish_baseline_mcp_policy(self) -> Path:
        return self.publish_contracts_dir / "policies" / "baseline-mcp-policy.xml"

    @property
    def publish_baseline_a2a_policy(self) -> Path:
        return self.publish_contracts_dir / "policies" / "baseline-a2a-policy.xml"

    # Alerts, upgrade and integration modules --------------------------------------------------
    @property
    def alerts_dir(self) -> Path:
        return self.infra / "app-insights-alert"

    @property
    def alerts_template(self) -> Path:
        return self.alerts_dir / "main.bicep"

    @property
    def gateway_upgrade_template(self) -> Path:
        return self.infra / "apim-gateway-upgrade" / "main.bicep"

    @property
    def foundry_integration_template(self) -> Path:
        return self.infra / "foundry-integration" / "main.bicep"

    @property
    def main_template(self) -> Path:
        return self.infra / "main.bicep"

    def contract_templates(self) -> dict[str, Path]:
        """Return the deployable contract templates keyed by contract kind."""
        return {
            "access": self.access_contract_template,
            "backend": self.backend_contract_template,
            "publish": self.publish_contract_template,
            "alerts": self.alerts_template,
        }

    def required_files(self) -> list[Path]:
        """Return every upstream file the agent depends on."""
        return [
            self.release_file,
            *self.contract_templates().values(),
            self.access_base_contract_dir / "main.bicepparam",
            self.access_base_contract_dir / "ai-product-policy.xml",
            self.access_default_policy,
            self.access_default_multi_policy,
            self.publish_baseline_mcp_policy,
            self.publish_baseline_a2a_policy,
            self.gateway_upgrade_template,
            self.main_template,
        ]

    def missing_files(self) -> list[Path]:
        """Return required files that do not exist (empty when the checkout is complete)."""
        return [path for path in self.required_files() if not path.is_file()]

    def documentation_files(self) -> list[Path]:
        """Return all Markdown documentation files in the upstream checkout, sorted by path."""
        docs = [
            path
            for path in self.root.rglob("*.md")
            if not _DOC_EXCLUDED_PARTS.intersection(path.relative_to(self.root).parts)
        ]
        return sorted(docs)

    def relative(self, path: Path) -> str:
        """Return ``path`` relative to the upstream root using forward slashes (for citations)."""
        return path.relative_to(self.root).as_posix()


def locate_upstream(root: Path | None = None) -> UpstreamLayout:
    """Return the layout of the pinned upstream checkout recorded in ``citadel.lock.json``.

    Args:
        root: Agent checkout root; discovered automatically when omitted.
    """
    project_root = root or find_project_root()
    lock = read_lock(project_root)
    return UpstreamLayout(project_root / lock.upstream.path)
