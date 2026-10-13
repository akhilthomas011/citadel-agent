---
name: alerts
description: Set up alert rules for throttling, quota exhaustion and failures, for one use case or the whole gateway.
metadata:
  persona: any
  docs: "bicep/infra/app-insights-alert/README.md#two-alerting-modes-alertmode bicep/infra/app-insights-alert/README.md#parameters guides/throttling-events-handling.md#event-categories--default-behavior"
---

# Alerts

## Overview

The gateway emits alert events (throttling, quota exhausted, backend failures and, when enabled per contract, authentication, content safety and PII failures). Alert rules turn these events into email or webhook notifications.

## Interview

1. Scope: one use case (its product) or the whole gateway. Gateway-wide alerts need the operator persona.
2. Which events matter, and who receives notifications (email addresses or an existing action group).
3. Sensitivity: how many events within what time window should trigger an alert.

## Workflow

1. For use-case alerts, check that the contract emits the needed event categories with `describe_access_contract`. Authentication, content safety and PII events are off by default; enabling them is an access contract change.
2. Call `draft_alert_rules` with the scope, events and recipients. Add the access contract change to the same request when categories must be enabled.
3. Prepare, explain the what-if, get approval and execute.
4. Explain how to test an alert, for example by exceeding a low tokens-per-minute limit in a dev environment.

## Decision rules

* Keep throttling, quota and backend failure alerts on for production use cases.
* Avoid alerting on every authentication failure for public clients; it is noisy.

## Guardrails

* Do not add recipients outside the user's organization without confirmation.
