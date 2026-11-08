# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""CSV presentation shared by legacy previews and immutable completed report exports."""

import csv
import io
import json

from citadel_agent.analytics.rollups import TOKEN_SOURCE_FIELDS, UsageDimension
from citadel_agent.portal.contracts import TokenUsage
from citadel_agent.portal.product_contracts import DashboardQuery, FinOpsResponse


def finops_csv(query: DashboardQuery, report: FinOpsResponse) -> dict[str, str]:
    if report.analysis is not None:
        return analysis_csv(report)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "start_utc",
            "end_utc",
            "dimension",
            "value",
            "prompt_tokens",
            "completion_tokens",
            "estimated_cost",
            "currency",
            "pricing_coverage_percent",
            "quality",
            "warnings",
            "report_id",
            "source_scan_complete",
            "observed_at",
            "input_kind",
            "read_model_version",
        ]
    )
    for bucket in report.aggregates:
        label = getattr(bucket.dimensions, query.group_by.value) or "Unattributed"
        safe_label = "'" + label if label.lstrip().startswith(("=", "+", "-", "@")) else label
        for cost in bucket.costs or [None]:
            writer.writerow(
                [
                    bucket.window.start.isoformat(),
                    bucket.window.end.isoformat(),
                    query.group_by.value,
                    safe_label,
                    bucket.usage.prompt,
                    bucket.usage.completion,
                    str(cost.amount) if cost else "",
                    cost.currency if cost else "",
                    str(bucket.pricing_coverage.priced_usage_percent),
                    report.quality.state.value,
                    ";".join(report.warnings + report.quality.warning_codes),
                    report.report_id or "",
                    report.source_scan_complete,
                    report.quality.observed_at.isoformat(),
                    report.input_kind,
                    report.read_model_version or "",
                ]
            )
    return {"filename": "citadel-finops.csv", "content": output.getvalue()}


def _safe_cell(value: str | None) -> str:
    if value is None:
        return ""
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def analysis_csv(report: FinOpsResponse) -> dict[str, str]:
    if report.analysis is None:
        raise ValueError("the completed report contains no analytical projections")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    dimensions = list(UsageDimension)
    tokens = list(TokenUsage.model_fields)
    writer.writerow(
        [
            "profile",
            "start_utc",
            "end_utc",
            "hour_utc",
            *(value.value for value in dimensions),
            "requests",
            "unpriced_requests",
            *tokens,
            "unavailable_token_fields",
            "estimated_cost",
            "currency",
            "pricing_coverage_percent",
            "quality",
            "report_id",
            "input_kind",
            "read_model_version",
            "observed_at",
            "warnings",
        ]
    )
    profiles = {**report.analysis.profiles, "hour_of_day": report.analysis.hour_of_day}
    for profile, rows in profiles.items():
        for row in rows:
            for cost in row.costs or [None]:
                writer.writerow(
                    [
                        profile,
                        row.window.start.isoformat(),
                        row.window.end.isoformat(),
                        row.hour_utc if row.hour_utc is not None else "",
                        *(_safe_cell(row.dimensions.get(value)) for value in dimensions),
                        row.requests,
                        row.unpriced_requests,
                        *(
                            getattr(row.usage, name)
                            if getattr(row.usage, name) is not None
                            and TOKEN_SOURCE_FIELDS[name] not in row.unavailable_tokens
                            else ""
                            for name in tokens
                        ),
                        json.dumps(row.unavailable_tokens),
                        str(cost.amount) if cost else "",
                        cost.currency if cost else "",
                        str(row.priced_usage_percent),
                        report.quality.state,
                        report.report_id,
                        report.input_kind,
                        report.read_model_version or "",
                        report.quality.observed_at.isoformat(),
                        ";".join(report.warnings + report.quality.warning_codes),
                    ]
                )
    return {"filename": "citadel-finops-analysis.csv", "content": output.getvalue()}
