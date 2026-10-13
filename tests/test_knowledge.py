# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for the knowledge layer: upstream doc search and the versioned catalogs."""

from __future__ import annotations

import typing
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic.alias_generators import to_camel

from citadel_agent.contracts.access import AccessContractIntent
from citadel_agent.knowledge import DocIndex, build_doc_index, load_catalogs
from citadel_agent.knowledge.docs import github_anchor, parse_markdown, tokenize
from citadel_agent.upstream.drift import check_knowledge
from citadel_agent.upstream.layout import UpstreamLayout

UPSTREAM = Path(__file__).resolve().parents[1] / "upstream" / "ai-hub-gateway"
needs_upstream = pytest.mark.skipif(
    not (UPSTREAM / "release.json").is_file(), reason="Upstream submodule is not checked out"
)

GUIDE = """---
title: Rotation guide
---
Intro text about keys.

# 🔑 Key Rotation Guide

Rotate the subscription key without downtime.

## Steps

```bash
# not a heading
az deployment sub create
```

### Step 1: Switch (usePrimaryKey=false)

Switch clients to the secondary key.

## Steps

Duplicate heading.
"""


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("🔑 Access Contract Key Rotation Guide", "-access-contract-key-rotation-guide"),
        ("`code` and [a link](https://x.example)", "code-and-a-link"),
        ("Step 2: Backend Pool (set-backend-pools)", "step-2-backend-pool-set-backend-pools"),
        ("**Bold** words", "bold-words"),
    ],
)
def test_github_anchor(heading: str, anchor: str) -> None:
    assert github_anchor(heading) == anchor


def test_parse_markdown_sections() -> None:
    sections = parse_markdown("guides/rotation.md", GUIDE)
    assert [s.heading for s in sections] == [
        "Rotation guide",
        "🔑 Key Rotation Guide",
        "Steps",
        "Step 1: Switch (usePrimaryKey=false)",
        "Steps",
    ]
    assert sections[0].anchor == "" and sections[0].citation == "guides/rotation.md"
    assert [s.anchor for s in sections[2:]] == ["steps", "step-1-switch-useprimarykeyfalse", "steps-1"]
    assert sections[3].breadcrumb == ("🔑 Key Rotation Guide", "Steps")
    assert "# not a heading" in sections[2].text


def test_tokenize_keeps_identifiers_and_their_parts() -> None:
    tokens = tokenize("Set allowedModels and x-ratelimit-remaining-tokens for the 429")
    assert {"allowedmodels", "allowed", "models", "x-ratelimit-remaining-tokens", "ratelimit", "429"} <= set(
        tokens
    )
    assert "the" not in tokens and "and" not in tokens


def test_search_ranks_sections_and_reads_them() -> None:
    index = DocIndex(
        parse_markdown("guides/rotation.md", GUIDE)
        + parse_markdown("guides/models.md", "# Models\n\nUse allowedModels to restrict models.\n"),
        base_url="https://github.com/o/r/blob/abc",
    )
    hits = index.search("switch the secondary key")
    assert hits[0].section.citation == "guides/rotation.md#step-1-switch-useprimarykeyfalse"
    assert (
        hits[0].url == "https://github.com/o/r/blob/abc/guides/rotation.md#step-1-switch-useprimarykeyfalse"
    )
    assert index.search("allowedModels", path_prefix="guides/models")[0].section.path == "guides/models.md"
    assert index.search("") == []

    text = index.read("guides/rotation.md#steps")
    assert text is not None and "Step 1: Switch" in text and "Duplicate heading" not in text
    assert index.read("guides/rotation.md#missing") is None
    assert index.read("guides/rotation.md#-key-rotation-guide", max_chars=40).endswith("[truncated]")


# Catalogs --------------------------------------------------------------------------------------


def test_catalog_ids_are_unique() -> None:
    catalogs = load_catalogs()
    for items in (catalogs.errors, catalogs.surfaces, catalogs.capabilities, catalogs.names, catalogs.noise):
        ids = [item.id for item in items]
        assert len(ids) == len(set(ids))
    assert len({v.name for v in catalogs.variables}) == len(catalogs.variables)
    assert catalogs.surface("unified-ai") is not None and catalogs.surface("unified-ai").recommended


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "status": 403,
                "body": '{"error": {"code": "unauthorized_model_access", "message": "Access to model x"}}',
            },
            "model-not-allowed",
        ),
        (
            {
                "status": 401,
                "body": '{"statusCode": 401, "message": "Access denied due to missing subscription key."}',
            },
            "missing-subscription-key",
        ),
        (
            {"status": 403, "body": {"statusCode": 403}, "headers": {"Retry-After": "60"}},
            "token-quota-exceeded",
        ),
        (
            {
                "status": 500,
                "body": '{"statusCode": 500, "message": "Internal server error"}',
                "log_message": "Backend service URL is not defined.",
            },
            "backend-url-not-defined",
        ),
        ({"status": 429, "body": "Token limit is exceeded."}, "rate-limited"),
        ({"status": 400, "body": {"error": {"code": "extraction_missing_model_parameter"}}}, "missing-model"),
        ({"status": 403, "body": "ForbiddenByFirewall", "surface": "azure"}, "key-vault-firewall"),
    ],
)
def test_error_matching(kwargs: dict[str, typing.Any], expected: str) -> None:
    matches = load_catalogs().match_errors(**kwargs)
    assert matches and matches[0].id == expected


def test_unexplained_errors_and_other_surfaces_do_not_match() -> None:
    catalogs = load_catalogs()
    assert catalogs.match_errors(status=403, body={"statusCode": 403, "message": "Forbidden"}) == []
    assert all(
        e.id != "key-vault-firewall" for e in catalogs.match_errors(status=403, body="ForbiddenByFirewall")
    )
    assert catalogs.match_errors(status=401, surface="mcp")[0].id == "mcp-tool-call-unauthorized"


def test_what_if_noise_classification() -> None:
    catalogs = load_catalogs()
    apim = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim"
    assert (
        catalogs.classify_noise(f"{apim}/products/p/apis/unified-ai-api", "Create").id
        == "apim-product-api-link"
    )
    policy = catalogs.classify_noise(
        f"{apim}/products/p/policies/policy",
        "Modify",
        ["properties.format (Modify)", "properties.value (Modify)"],
    )
    assert policy is not None and policy.id == "apim-rawxml-policy"
    subscription = f"{apim}/subscriptions/p-SUB-01"
    assert catalogs.classify_noise(subscription, "Modify", ["properties.allowTracing (Delete)"]) is not None
    assert catalogs.classify_noise(subscription, "Modify", ["properties.state (Modify)"]) is None
    assert catalogs.classify_noise(f"{apim}/products/p", "Delete") is None


def _field_model(annotation: typing.Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in typing.get_args(annotation):
        found = _field_model(argument)
        if found is not None:
            return found
    return None


def test_policy_variables_map_to_access_intent_fields() -> None:
    for variable in load_catalogs().variables:
        if variable.intent_field.startswith("("):
            continue
        model: type[BaseModel] | None = AccessContractIntent
        for segment in variable.intent_field.split("."):
            assert model is not None, variable.intent_field
            name = segment.removesuffix("[]")
            field = next(
                (f for attr, f in model.model_fields.items() if name in {f.alias, to_camel(attr)}),
                None,
            )
            assert field is not None, (
                f"{variable.name}: {variable.intent_field} ({name} not in {model.__name__})"
            )
            model = _field_model(field.annotation)


@needs_upstream
def test_doc_index_over_the_pinned_upstream() -> None:
    index = build_doc_index(UPSTREAM)
    assert len(index.files) >= 30
    top = [hit.section.path for hit in index.search("rotate subscription key", limit=3)]
    assert any("key-rotation" in path for path in top)


@needs_upstream
def test_catalogs_match_the_pinned_upstream() -> None:
    assert check_knowledge(UpstreamLayout(UPSTREAM)) == []
