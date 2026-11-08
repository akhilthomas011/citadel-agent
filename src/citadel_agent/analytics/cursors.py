# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Signed, expiring Cosmos cursors bound to an authorized hub and query."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, ValidationError

from citadel_agent.contracts.common import CitadelModel
from citadel_agent.portal.contracts import HubScope

_MAX_CURSOR_LENGTH = 131_072
_MAX_CONTINUATION_LENGTH = 65_536


class CursorValidationError(ValueError):
    """A cursor is malformed, expired, or does not match its authorized query."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Cosmos cursor is invalid ({code}).")
        self.code = code


class _CursorPayload(CitadelModel):
    version: Literal[1] = 1
    scope: HubScope
    query_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    continuation_token: str = Field(min_length=1, max_length=_MAX_CONTINUATION_LENGTH)
    expires_at: int = Field(gt=0)


def cosmos_query_fingerprint(
    sql: str,
    parameters: Mapping[str, object] | None,
    *,
    account_endpoint: str,
    database: str,
    container: str,
    page_size: int,
) -> str:
    """Hash account, container, query text, normalized parameters, and page size."""
    if not sql.strip():
        raise ValueError("Cosmos query text must not be empty")
    if not account_endpoint.strip() or not database.strip() or not container.strip():
        raise ValueError("Cosmos account, database, and container must not be empty")
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")

    normalized_parameters: dict[str, object] = {}
    for name, value in (parameters or {}).items():
        normalized_name = name if name.startswith("@") else f"@{name}"
        if normalized_name == "@":
            raise ValueError("Cosmos query parameter names must not be empty")
        if normalized_name in normalized_parameters:
            raise ValueError("Cosmos query parameter names must be unique")
        normalized_parameters[normalized_name] = value

    canonical_query = json.dumps(
        {
            "accountEndpoint": account_endpoint.rstrip("/").casefold(),
            "database": database,
            "container": container,
            "sql": sql,
            "parameters": [
                {"name": name, "value": value} for name, value in sorted(normalized_parameters.items())
            ],
            "pageSize": page_size,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_query.encode("utf-8")).hexdigest()


class ScopedCosmosCursorSigner:
    """Sign continuation tokens and enforce scope, query, page-size, and expiry binding."""

    def __init__(self, signing_key: bytes, *, ttl_seconds: int = 900) -> None:
        if len(signing_key) < 32:
            raise ValueError("cursor signing key must contain at least 32 bytes")
        if not 60 <= ttl_seconds <= 3600:
            raise ValueError("cursor TTL must be between 60 and 3600 seconds")
        self._signing_key = signing_key
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        continuation_token: str,
        *,
        scope: HubScope,
        query_fingerprint: str,
        now: datetime | None = None,
    ) -> str:
        """Create a tamper-evident cursor for one query continuation."""
        if not continuation_token or len(continuation_token) > _MAX_CONTINUATION_LENGTH:
            raise ValueError("Cosmos continuation token is empty or too large")
        issued_at = _utc_now(now)
        payload = _CursorPayload(
            scope=scope,
            query_fingerprint=query_fingerprint,
            continuation_token=continuation_token,
            expires_at=int(issued_at.timestamp()) + self.ttl_seconds,
        )
        encoded_payload = _encode(payload.model_dump_json(by_alias=True).encode("utf-8"))
        signature = hmac.new(
            self._signing_key,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_encode(signature)}"

    def read(
        self,
        cursor: str,
        *,
        scope: HubScope,
        query_fingerprint: str,
        now: datetime | None = None,
    ) -> str:
        """Validate a cursor and return its Cosmos continuation token."""
        if not cursor or len(cursor) > _MAX_CURSOR_LENGTH or cursor.count(".") != 1:
            raise CursorValidationError("malformed")
        encoded_payload, encoded_signature = cursor.split(".", maxsplit=1)
        try:
            signature = _decode(encoded_signature)
            payload_bytes = _decode(encoded_payload)
        except (ValueError, binascii.Error) as exc:
            raise CursorValidationError("malformed") from exc

        expected_signature = hmac.new(
            self._signing_key,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise CursorValidationError("signature-invalid")
        try:
            payload = _CursorPayload.model_validate_json(payload_bytes)
        except ValidationError as exc:
            raise CursorValidationError("malformed") from exc

        current_time = _utc_now(now)
        if payload.expires_at <= int(current_time.timestamp()):
            raise CursorValidationError("expired")
        if payload.scope != scope:
            raise CursorValidationError("scope-mismatch")
        if payload.query_fingerprint != query_fingerprint:
            raise CursorValidationError("query-mismatch")
        return payload.continuation_token


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("cursor timestamps must include a timezone")
    return current.astimezone(UTC)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
