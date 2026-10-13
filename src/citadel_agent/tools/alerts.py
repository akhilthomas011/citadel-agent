# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Alert rules tool: notify teams about throttling, quota exhaustion and failures.

(No postponed annotations; see ``tools.core``.)
"""

from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from citadel_agent.contracts.alerts import AlertRule, AlertsIntent
from citadel_agent.contracts.intent_io import parse_intent
from citadel_agent.policy.admin import Operation
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec

AlertEvent = Literal[
    "throttling", "quota-exceeded", "backend-failure", "auth-failure", "content-safety", "pii-failure"
]
DEFAULT_EVENTS = ["throttling", "quota-exceeded", "backend-failure"]
# Default rule per event: (severity, threshold). A rule fires when the event count in the window exceeds it.
RULE_DEFAULTS: dict[str, tuple[int, float]] = {
    "throttling": (2, 10),
    "quota-exceeded": (2, 0),
    "backend-failure": (1, 5),
    "auth-failure": (3, 20),
    "content-safety": (2, 0),
    "pii-failure": (1, 0),
}
# Access contract alert toggles (policy.alerts) per event.
CONTRACT_TOGGLES = {
    "throttling": "throttling",
    "quota-exceeded": "quotaExceeded",
    "backend-failure": "backendFailure",
    "auth-failure": "authFailure",
    "content-safety": "contentSafety",
    "pii-failure": "piiFailure",
}


def _existing_alerts(ctx: ToolContext, scope: str, env_name: str) -> AlertsIntent | None:
    for record in ctx.repository().intents("alerts"):
        intent = record.intent
        if intent.environment == env_name and intent.scope_name.lower() == scope.lower():
            return intent
    return None


def draft_alert_rules(
    ctx: ToolContext,
    email: str,
    environment: str | None = None,
    business_unit: str | None = None,
    use_case: str | None = None,
    events: list[str] | None = None,
    additional_emails: list[str] | None = None,
    window_minutes: int | None = None,
    draft_id: str | None = None,
) -> str:
    env_name = ctx.environment_name(environment)
    ctx.environment(env_name)
    authorizer = ctx.runtime.authorizer
    authorizer.require(Operation.ALERTS_WRITE)
    events = list(dict.fromkeys(events or DEFAULT_EVENTS))
    contract = None
    if business_unit and use_case:
        contract = ctx.owned_access_contract(business_unit, use_case, env_name)
        scope = f"{contract.use_case.business_unit}-{contract.use_case.use_case_name}".lower()
        product_filter = contract.product_display_name(contract.services[0].code)
    elif ctx.persona == "operator":
        scope, product_filter = "gateway", ""
    else:
        raise ToolInputError(
            "Name your use case (business_unit and use_case); gateway-wide alerts need the platform team"
        )
    window = f"PT{window_minutes}M" if window_minutes else "PT5M"
    rules = [
        AlertRule(
            name=event,
            alert_type=event,  # type: ignore[arg-type]
            severity=RULE_DEFAULTS[event][0],
            threshold=RULE_DEFAULTS[event][1],
            window_size=window,
        ).model_dump(by_alias=True)
        for event in events
    ]
    existing = _existing_alerts(ctx, scope, env_name)
    receivers = [
        {"name": f"recipient-{index + 1}", "email": address}
        for index, address in enumerate(additional_emails or [])
    ]
    data: dict[str, Any] = (
        existing.model_dump(mode="json", by_alias=True, exclude_none=True)
        if existing
        else {
            "kind": "alerts",
            "environment": env_name,
            "scopeName": scope,
            "metadata": {"owners": [ctx.caller]},
        }
    )
    data.update(
        {
            "productDisplayNameFilter": product_filter,
            "alertEmailAddress": email,
            "additionalEmailReceivers": receivers or data.get("additionalEmailReceivers", []),
            "alerts": rules,
        }
    )
    try:
        intent = parse_intent(data)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        raise ToolInputError(f"The alert rules are not valid: {details}") from exc
    draft = ctx.drafts.save(
        intent,
        operation=Operation.ALERTS_WRITE,
        created_by=ctx.caller,
        step_id=f"alerts-{scope}"[:40],
        draft_id=draft_id,
    )
    target = "the whole gateway" if scope == "gateway" else product_filter
    lines = [
        f"Alert rules drafted (draft_id: {draft.id}) for {target}.",
        *[
            f"* {r['name']}: more than {r['threshold']:g} events in {window}, severity {r['severity']}"
            for r in rules
        ],
        f"* Notifications to {', '.join([email, *(additional_emails or [])])}",
    ]
    draft_ids = [draft.id]
    if contract is not None:
        toggles = contract.model_dump(mode="json", by_alias=True)["services"][0]["policy"].get("alerts", {})
        missing = [e for e in events if not toggles.get(CONTRACT_TOGGLES[e], False)]
        if missing or not toggles.get("enabled", True):
            access_data = contract.model_dump(mode="json", by_alias=True, exclude_none=True)
            for service in access_data["services"]:
                alerts = service.setdefault("policy", {}).setdefault("alerts", {})
                alerts["enabled"] = True
                for event in missing:
                    alerts[CONTRACT_TOGGLES[event]] = True
            access_draft = ctx.drafts.save(
                parse_intent(access_data), operation=Operation.ACCESS_WRITE, created_by=ctx.caller
            )
            draft_ids.append(access_draft.id)
            lines.append(
                f"* The access contract does not emit {', '.join(missing) or 'alert'} events yet; "
                f"drafted that change too (draft_id: {access_draft.id})."
            )
    lines.append(
        "Nothing is deployed yet. Confirm with the user, then call prepare_change with draft_ids "
        f"{draft_ids}. "
        "Azure Monitor emails each recipient once when it is added to the action group."
    )
    return "\n".join(lines)


def register_alert_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def draft_alert_rules_tool(
        email: Annotated[str, Field(description="Email address that receives the alerts.")],
        environment: Annotated[
            str | None, Field(description="Environment; defaults to the current one.")
        ] = None,
        business_unit: Annotated[
            str | None, Field(description="Business unit (omit for gateway-wide alerts).")
        ] = None,
        use_case: Annotated[
            str | None, Field(description="Use case name (omit for gateway-wide alerts).")
        ] = None,
        events: Annotated[list[AlertEvent] | None, Field(description="Events to alert on.")] = None,
        additional_emails: Annotated[list[str] | None, Field(description="More recipients.")] = None,
        window_minutes: Annotated[int | None, Field(ge=1, le=1440, description="Evaluation window.")] = None,
        draft_id: Annotated[
            str | None, Field(description="Revise this draft instead of creating a new one.")
        ] = None,
    ) -> str:
        return draft_alert_rules(
            ctx,
            email,
            environment,
            business_unit,
            use_case,
            events,
            additional_emails,
            window_minutes,
            draft_id,
        )

    registry.add(
        ToolSpec(
            "draft_alert_rules",
            draft_alert_rules_tool,
            "Draft Azure Monitor alert rules for a use case (or, for operators, the whole gateway), plus the "
            "access contract change that enables the events. Changes nothing until prepared and approved.",
        )
    )
