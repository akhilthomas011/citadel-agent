# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Git hosting providers used to open and track pull requests for change sets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from citadel_agent.safety.redaction import default_redactor


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    url: str
    state: str
    merged: bool
    head_sha: str | None = None
    checks: str | None = None
    files: tuple[str, ...] = ()


class GitProviderError(RuntimeError):
    """Raised when the git hosting provider rejects a request or is not configured."""


class GitProvider(Protocol):
    def open_pull_request(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
        labels: list[str],
        reviewers: list[str],
    ) -> PullRequestInfo: ...

    def pull_request_status(self, number: int) -> PullRequestInfo: ...

    def open_pull_requests(self, *, labels: list[str]) -> list[PullRequestInfo]: ...


def github_token() -> str | None:
    """Return a GitHub token (registered for redaction), kept in memory only.

    Uses ``GITHUB_TOKEN`` or ``GH_TOKEN``, otherwise the GitHub CLI's token when the user is signed
    in with ``gh auth login``.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or _gh_cli_token()
    default_redactor().register_secret(token)
    return token


def _gh_cli_token() -> str | None:
    from citadel_agent.proc import CommandError, run_command

    try:
        result = run_command("gh", ["auth", "token"], check=False, timeout=30)
    except (CommandError, OSError):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def _summarize_checks(runs: list[Any]) -> str:
    if not runs:
        return "no checks"
    conclusions = [getattr(run, "conclusion", None) or getattr(run, "status", "unknown") for run in runs]
    if all(c == "success" for c in conclusions):
        return "success"
    if any(c in {"failure", "cancelled", "timed_out", "action_required"} for c in conclusions):
        return "failure"
    return "pending"


class GitHubProvider:
    """Pull requests on GitHub through the REST API (githubkit)."""

    def __init__(self, repository: str, *, token: str | None = None, client: Any = None) -> None:
        owner, _, name = repository.partition("/")
        if not owner or not name:
            raise GitProviderError(f"Repository must be 'owner/name', got '{repository}'")
        self.owner, self.name = owner, name
        if client is None:
            resolved = token or github_token()
            if not resolved:
                raise GitProviderError("Set GITHUB_TOKEN (or GH_TOKEN) to open pull requests on GitHub")
            from githubkit import GitHub

            client = GitHub(resolved)
        self._client = client

    def open_pull_request(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
        labels: list[str],
        reviewers: list[str],
    ) -> PullRequestInfo:
        rest = self._client.rest
        response = rest.pulls.create(
            self.owner, self.name, data={"title": title, "head": branch, "base": base, "body": body}
        )
        pr = response.parsed_data
        if labels:
            rest.issues.add_labels(self.owner, self.name, pr.number, data={"labels": labels})
        users = [r for r in reviewers if "/" not in r]
        teams = [r.split("/", 1)[1] for r in reviewers if "/" in r]
        if users or teams:
            rest.pulls.request_reviewers(
                self.owner, self.name, pr.number, data={"reviewers": users, "team_reviewers": teams}
            )
        return PullRequestInfo(
            number=pr.number, url=pr.html_url, state=pr.state, merged=False, head_sha=pr.head.sha
        )

    def pull_request_status(self, number: int) -> PullRequestInfo:
        rest = self._client.rest
        pr = rest.pulls.get(self.owner, self.name, number).parsed_data
        runs = rest.checks.list_for_ref(self.owner, self.name, pr.head.sha).parsed_data.check_runs
        return PullRequestInfo(
            number=pr.number,
            url=pr.html_url,
            state=pr.state,
            merged=bool(pr.merged),
            head_sha=pr.head.sha,
            checks=_summarize_checks(list(runs)),
        )

    def open_pull_requests(self, *, labels: list[str], limit: int = 50) -> list[PullRequestInfo]:
        """Open pull requests carrying all ``labels``, with the files each one changes."""
        rest = self._client.rest
        try:
            issues = rest.issues.list_for_repo(
                self.owner, self.name, state="open", labels=",".join(labels), per_page=limit
            ).parsed_data
            result: list[PullRequestInfo] = []
            for issue in issues:
                if not getattr(issue, "pull_request", None):
                    continue
                files = rest.pulls.list_files(self.owner, self.name, issue.number, per_page=100).parsed_data
                result.append(
                    PullRequestInfo(
                        number=issue.number,
                        url=issue.html_url,
                        state=issue.state,
                        merged=False,
                        files=tuple(f.filename for f in files),
                    )
                )
            return result
        except Exception as exc:
            raise GitProviderError(default_redactor().redact(str(exc))[:300]) from exc


class AzureDevOpsProvider:
    """Placeholder for Azure Repos support (planned)."""

    def __init__(self, *_: Any, **__: Any) -> None:
        raise GitProviderError("Azure DevOps support is planned; use the GitHub provider for now")
