# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Read-only access to the Citadel API Management instance (management plane).

Listing methods never return secret values. :meth:`ApimClient.subscription_keys_in_memory` is the
only method that reads keys; it registers them with the redactor so they cannot leak into tool
output, logs or audit records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from azure.core.credentials import TokenCredential

from citadel_agent.safety.redaction import Redactor, default_redactor


@dataclass(frozen=True)
class ApimServiceInfo:
    name: str
    gateway_url: str | None
    sku: str | None
    location: str | None
    user_assigned_identity_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApiInfo:
    name: str
    display_name: str | None
    path: str | None
    api_type: str | None
    subscription_required: bool | None


@dataclass(frozen=True)
class ProductInfo:
    name: str
    display_name: str | None
    state: str | None
    subscription_required: bool | None


@dataclass(frozen=True)
class SubscriptionInfo:
    name: str
    display_name: str | None
    scope: str | None
    state: str | None

    @property
    def product_id(self) -> str | None:
        """Product name when the subscription is scoped to a product."""
        if self.scope and "/products/" in self.scope:
            return self.scope.rsplit("/products/", 1)[1].split("/")[0]
        return None


@dataclass(frozen=True)
class BackendInfo:
    name: str
    url: str | None
    backend_type: str | None
    pool_members: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class NamedValueInfo:
    name: str
    display_name: str | None
    secret: bool
    key_vault_secret_id: str | None


def _attr(obj: Any, *names: str) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


class ApimClient:
    """Management-plane reader for one API Management instance."""

    def __init__(
        self,
        credential: TokenCredential,
        subscription_id: str,
        resource_group: str,
        service_name: str,
        *,
        client: Any = None,
        redactor: Redactor | None = None,
    ) -> None:
        if client is None:
            from azure.mgmt.apimanagement import ApiManagementClient

            client = ApiManagementClient(credential, subscription_id)
        self._client = client
        self.resource_group = resource_group
        self.service_name = service_name
        self._redactor = redactor or default_redactor()

    @property
    def _scope(self) -> tuple[str, str]:
        return self.resource_group, self.service_name

    def service(self) -> ApimServiceInfo:
        svc = self._client.api_management_service.get(*self._scope)
        identity = getattr(svc, "identity", None)
        assigned = getattr(identity, "user_assigned_identities", None) or {}
        return ApimServiceInfo(
            name=svc.name,
            gateway_url=getattr(svc, "gateway_url", None),
            sku=getattr(getattr(svc, "sku", None), "name", None),
            location=getattr(svc, "location", None),
            user_assigned_identity_ids=tuple(assigned.keys()),
        )

    def list_apis(self) -> list[ApiInfo]:
        apis = []
        for api in self._client.api.list_by_service(*self._scope):
            if getattr(api, "is_current", True) is False:
                continue
            apis.append(
                ApiInfo(
                    name=api.name,
                    display_name=_attr(api, "display_name"),
                    path=_attr(api, "path"),
                    api_type=_attr(api, "api_type", "type_properties_type"),
                    subscription_required=_attr(api, "subscription_required"),
                )
            )
        return apis

    def list_products(self) -> list[ProductInfo]:
        return [
            ProductInfo(
                name=p.name,
                display_name=_attr(p, "display_name"),
                state=str(_attr(p, "state")) if _attr(p, "state") is not None else None,
                subscription_required=_attr(p, "subscription_required"),
            )
            for p in self._client.product.list_by_service(*self._scope)
        ]

    def get_product_policy(self, product_id: str) -> str | None:
        """Return the product policy XML, or None when the product has no policy."""
        from azure.core.exceptions import ResourceNotFoundError

        try:
            policy = self._client.product_policy.get(*self._scope, product_id, "policy", format="rawxml")
        except ResourceNotFoundError:
            return None
        return getattr(policy, "value", None)

    def list_product_apis(self, product_id: str) -> list[str]:
        return [api.name for api in self._client.product_api.list_by_product(*self._scope, product_id)]

    def list_subscriptions(self) -> list[SubscriptionInfo]:
        """List subscriptions (keys are never included)."""
        return [
            SubscriptionInfo(
                name=s.name,
                display_name=_attr(s, "display_name"),
                scope=_attr(s, "scope"),
                state=str(_attr(s, "state")) if _attr(s, "state") is not None else None,
            )
            for s in self._client.subscription.list(*self._scope)
        ]

    def subscription_keys_in_memory(self, subscription_id: str) -> tuple[str, str]:
        """Return ``(primary, secondary)`` keys for in-memory use only. Both are registered for redaction."""
        secrets = self._client.subscription.list_secrets(*self._scope, subscription_id)
        primary = getattr(secrets, "primary_key", "") or ""
        secondary = getattr(secrets, "secondary_key", "") or ""
        self._redactor.register_secrets([primary, secondary])
        return primary, secondary

    def list_backends(self) -> list[BackendInfo]:
        backends = []
        for b in self._client.backend.list_by_service(*self._scope):
            pool = getattr(b, "pool", None)
            services = getattr(pool, "services", None) or []
            backends.append(
                BackendInfo(
                    name=b.name,
                    url=_attr(b, "url"),
                    backend_type=str(_attr(b, "type_properties_type", "backend_type")) if pool else "Single",
                    pool_members=tuple(str(getattr(s, "id", "")).rsplit("/", 1)[-1] for s in services),
                )
            )
        return backends

    def list_named_values(self) -> list[NamedValueInfo]:
        """List named values without their values."""
        values = []
        for nv in self._client.named_value.list_by_service(*self._scope):
            key_vault = getattr(nv, "key_vault", None)
            values.append(
                NamedValueInfo(
                    name=nv.name,
                    display_name=_attr(nv, "display_name"),
                    secret=bool(_attr(nv, "secret")),
                    key_vault_secret_id=getattr(key_vault, "secret_identifier", None),
                )
            )
        return values

    def get_named_value(self, named_value_id: str) -> str | None:
        """Return a non-secret named value. Secret named values are never returned (None)."""
        nv = self._client.named_value.get(*self._scope, named_value_id)
        if _attr(nv, "secret"):
            return None
        return _attr(nv, "value")

    def list_policy_fragments(self) -> list[str]:
        fragments = self._client.policy_fragment.list_by_service(*self._scope)
        items = getattr(fragments, "value", fragments)
        return sorted(f.name for f in items)
