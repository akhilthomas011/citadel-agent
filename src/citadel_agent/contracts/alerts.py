# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Alert intent (``bicep/infra/app-insights-alert``): Azure Monitor rules on the AI Gateway Alert metric."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from citadel_agent.contracts.common import (
    CitadelModel,
    ContractMetadata,
    Iso8601Duration,
    check_deployment_name,
    check_secret_ingress,
)

AlertType = Literal[
    "throttling", "quota-exceeded", "backend-failure", "auth-failure", "content-safety", "pii-failure"
]
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AlertRule(CitadelModel):
    name: str = Field(pattern=r"^[A-Za-z0-9-]{1,60}$")
    alert_type: AlertType
    severity: int = Field(default=2, ge=0, le=4)
    operator: Literal["GreaterThan", "GreaterThanOrEqual", "LessThan", "LessThanOrEqual", "Equals"] = (
        "GreaterThan"
    )
    threshold: float = Field(default=0, ge=0)
    time_aggregation: Literal["Total", "Count", "Average", "Maximum", "Minimum"] = "Total"
    window_size: Iso8601Duration = "PT5M"
    evaluation_frequency: Iso8601Duration = "PT1M"
    description: str = ""


class EmailReceiver(CitadelModel):
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(pattern=EMAIL_PATTERN)


def default_alert_rules() -> list[AlertRule]:
    """The upstream default rules: throttling, quota-exceeded and backend-failure."""
    return [
        AlertRule(name="throttling", alert_type="throttling", severity=2, threshold=10),
        AlertRule(name="quota-exceeded", alert_type="quota-exceeded", severity=2, threshold=0),
        AlertRule(name="backend-failure", alert_type="backend-failure", severity=1, threshold=5),
    ]


class AlertsIntent(CitadelModel):
    """Intent for alert rules (``contracts/alerts/<scope>/<env>/contract.yaml``)."""

    kind: Literal["alerts"] = "alerts"
    schema_version: Literal[1] = 1
    environment: str = Field(min_length=1)
    scope_name: str = Field(
        pattern=r"^[A-Za-z0-9-]{1,60}$", description="'gateway' or an access-contract product"
    )
    metadata: ContractMetadata = Field(default_factory=ContractMetadata)
    product_display_name_filter: str = Field(
        default="", description="APIM product DISPLAY name (space separated), empty for the whole gateway"
    )
    alert_email_address: str = Field(pattern=EMAIL_PATTERN)
    additional_email_receivers: list[EmailReceiver] = Field(default_factory=list)
    alert_mode: Literal["logQuery", "metric"] = "logQuery"
    name_prefix: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9-]{1,40}$",
        description="Prefix for rule and action-group names; defaults to a value unique to the scope",
    )
    action_group_short_name: str = Field(default="aigwalerts", min_length=1, max_length=12)
    alerts_enabled: bool = True
    alerts: list[AlertRule] = Field(default_factory=default_alert_rules, min_length=1)
    extra_parameters: dict[str, object] = Field(default_factory=dict)

    @property
    def effective_name_prefix(self) -> str:
        """Rule names are '<prefix>-<rule>-alert', so each scope needs its own prefix to avoid overwrites."""
        if self.name_prefix:
            return self.name_prefix
        return "ai-gateway" if self.scope_name == "gateway" else f"aigw-{self.scope_name.lower()}"

    @model_validator(mode="after")
    def _validate_alerts(self) -> AlertsIntent:
        names = [a.name for a in self.alerts]
        if len(names) != len(set(names)):
            raise ValueError("Alert rule names must be unique")
        if "-" in self.product_display_name_filter and " " not in self.product_display_name_filter:
            raise ValueError(
                "productDisplayNameFilter must be the product DISPLAY name with spaces "
                "(for example 'LLM HR ChatAgent DEV'), not the hyphenated product id"
            )
        prefix = self.effective_name_prefix
        for rule in self.alerts:
            # Upstream nests deployments named 'deploy-<prefix>-logalert-<rule>' (64-character limit).
            check_deployment_name(f"deploy-{prefix}-logalert-{rule.name}")
        check_secret_ingress(self, "Alerts intent")
        return self
