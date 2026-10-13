# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Key rotation tools: start and advance a verified, zero-downtime rotation.

Each step is prepared as a normal change set (approved and executed with ``execute_change``).
The next step is prepared only after the previous deployment's outputs are verified in Azure.
(No postponed annotations; see ``tools.core``.)
"""

import json
import logging
from typing import Annotated, Any

from pydantic import Field

from citadel_agent.azure_clients.deployments import DeploymentRecord, DeploymentRunner, DeploymentTarget
from citadel_agent.changes.engine import ChangeError, ChangeRequest, IntentChange
from citadel_agent.contracts.intent_io import intent_directory
from citadel_agent.policy.admin import Operation
from citadel_agent.rotation import (
    RotationError,
    RotationState,
    advance,
    apply,
    dump_state,
    read_rotation_state,
    rotation_path,
    start,
    verify,
)
from citadel_agent.tools.context import ToolContext
from citadel_agent.tools.registry import ToolInputError, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

STEP_SUMMARIES = {
    "switch": "Key rotation step 1 of 3 for {product}: publish the {to} key to consumers",
    "rotate": "Key rotation step 2 of 3 for {product}: regenerate the old {old} key (irreversible)",
    "maintenance": "Key rotation step 3 of 3 for {product}: switch key regeneration off",
    "rolled-back": "Roll back the key rotation for {product}: publish the {old} key again",
}


def _prepare(ctx: ToolContext, intent: Any, state: RotationState) -> str:
    product = intent.product_id(intent.services[0].code)
    summary = STEP_SUMMARIES[state.phase].format(product=product, to=state.to_slot, old=state.from_slot)
    request = ChangeRequest(
        summary=summary,
        environment=intent.environment,
        changes=(IntentChange(f"rotation-{state.phase}", apply(intent, state), Operation.ROTATION_WRITE),),
        extra_files=((rotation_path(state.contract_dir), dump_state(state)),),
    )
    try:
        prepared = ctx.engine().prepare(request)
    except ChangeError as exc:
        raise ToolInputError(f"The rotation step could not be prepared: {exc}") from exc
    return (
        f"{prepared.summary()}\n\nchange_id: {prepared.change.id}\napproval_hash: {prepared.content_hash}\n"
        "Nothing has changed yet. Explain this step to the user and ask for approval before calling "
        "execute_change."
    )


def _deployment(ctx: ToolContext, state: RotationState) -> DeploymentRecord | None:
    """The Azure deployment of the rotation step ``state.phase`` (local change sets, then manifests)."""
    step_id = f"rotation-{state.phase}"
    intent_file = f"{state.contract_dir}/contract.yaml"
    candidates: list[dict[str, Any]] = []
    store = ctx.runtime.store
    for change_id in store.list_ids():
        change = store.load(change_id)
        if intent_file in change.intents:
            candidates += [
                {"id": change.id, "name": s.deployment_name, "sub": s.subscription_id, "location": s.location}
                for s in change.steps
                if s.id == step_id
            ]
    manifests = ctx.repository().path(".citadel/changesets")
    if manifests.is_dir():
        for path in manifests.glob("cs-*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if intent_file in data.get("intents", []):
                candidates += [
                    {
                        "id": data["id"],
                        "name": s.get("deploymentName"),
                        "sub": s.get("subscriptionId"),
                        "location": s.get("location"),
                    }
                    for s in data.get("steps", [])
                    if s.get("id") == step_id
                ]
    for candidate in sorted(candidates, key=lambda c: c["id"], reverse=True):
        if not candidate["name"] or not candidate["sub"]:
            continue
        target = DeploymentTarget(
            scope="subscription", subscription_id=candidate["sub"], location=candidate["location"]
        )
        try:
            return DeploymentRunner(ctx.runtime.credential, candidate["sub"]).get(target, candidate["name"])
        except Exception:
            # Not deployed (yet) or not visible: try the next candidate.
            logger.debug("Deployment %s not found", candidate["name"], exc_info=True)
            continue
    return None


def start_key_rotation(
    ctx: ToolContext, business_unit: str, use_case: str, environment: str | None = None
) -> str:
    env_name = ctx.environment_name(environment)
    intent = ctx.owned_access_contract(business_unit, use_case, env_name)
    contract_dir = str(intent_directory(intent))
    existing = read_rotation_state(ctx.repository().root, contract_dir)
    try:
        state = start(intent, contract_dir, ctx.caller, existing)
    except RotationError as exc:
        raise ToolInputError(str(exc)) from exc
    return (
        _prepare(ctx, intent, state)
        + "\nAfter it is deployed, ask the owners to confirm that every client reads the new key from Key "
        "Vault, then call advance_key_rotation with owners_confirmed=true."
    )


def advance_key_rotation(
    ctx: ToolContext,
    business_unit: str,
    use_case: str,
    environment: str | None = None,
    owners_confirmed: bool = False,
    rollback: bool = False,
) -> str:
    env_name = ctx.environment_name(environment)
    intent = ctx.owned_access_contract(business_unit, use_case, env_name)
    contract_dir = str(intent_directory(intent))
    state = read_rotation_state(ctx.repository().root, contract_dir)
    if state is None:
        raise ToolInputError("No key rotation is recorded for this contract; use start_key_rotation")
    record = _deployment(ctx, state)
    if record is None:
        raise ToolInputError(
            f"The '{state.phase}' step is not deployed yet. In pull request mode, merge its pull request and "
            "wait for the pipeline; in direct mode, execute the prepared change first."
        )
    if not record.succeeded:
        raise ToolInputError(f"The '{state.phase}' deployment did not succeed ({record.provisioning_state})")
    problems = verify(state, dict(record.outputs))
    if problems:
        raise ToolInputError(
            f"The '{state.phase}' deployment does not show the expected result: {'; '.join(problems)}"
        )
    if state.finished:
        outcome = "rolled back" if state.phase == "rolled-back" else "complete"
        return (
            f"The key rotation is {outcome} and verified: the contract publishes the "
            f"{state.from_slot if state.phase == 'rolled-back' else state.to_slot} key."
        )
    if state.phase == "switch" and not rollback and not owners_confirmed:
        raise ToolInputError(
            "The switch is verified. Ask the owners to confirm every client now uses the new key, then call "
            "again with owners_confirmed=true (or rollback=true to undo)."
        )
    try:
        next_state = advance(state, rollback=rollback)
    except RotationError as exc:
        raise ToolInputError(str(exc)) from exc
    return _prepare(ctx, intent, next_state)


def register_rotation_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    def start_key_rotation_tool(
        business_unit: Annotated[str, Field(description="Business unit of the use case.")],
        use_case: Annotated[str, Field(description="Use case name.")],
        environment: Annotated[
            str | None, Field(description="Environment; defaults to the current one.")
        ] = None,
    ) -> str:
        return start_key_rotation(ctx, business_unit, use_case, environment)

    def advance_key_rotation_tool(
        business_unit: Annotated[str, Field(description="Business unit of the use case.")],
        use_case: Annotated[str, Field(description="Use case name.")],
        environment: Annotated[
            str | None, Field(description="Environment; defaults to the current one.")
        ] = None,
        owners_confirmed: Annotated[
            bool, Field(description="True only after the owners said every client uses the new key.")
        ] = False,
        rollback: Annotated[
            bool, Field(description="Undo the switch (only before the old key is regenerated).")
        ] = False,
    ) -> str:
        return advance_key_rotation(ctx, business_unit, use_case, environment, owners_confirmed, rollback)

    registry.add(
        ToolSpec(
            "start_key_rotation",
            start_key_rotation_tool,
            "Prepare step 1 of a zero-downtime key rotation (publish the other key). Changes nothing yet.",
        )
    )
    registry.add(
        ToolSpec(
            "advance_key_rotation",
            advance_key_rotation_tool,
            "Verify the last rotation step in Azure and prepare the next one (or a rollback).",
        )
    )
