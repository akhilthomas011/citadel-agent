# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for ``citadel-agent upstream sync``: diffs, verdicts, report, orchestration and CLI.

Upstream is faked: ``sync.git`` is replaced by an in-memory fake that serves zip archives of small
trees, so no network, real submodule or Bicep CLI is involved.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from citadel_agent import paths
from citadel_agent.cli import app
from citadel_agent.knowledge.docs import parse_markdown
from citadel_agent.proc import CommandError
from citadel_agent.upstream import UpstreamLock, UpstreamPin, read_lock, sync, write_lock
from citadel_agent.upstream.compat import CompatLevel
from citadel_agent.upstream.drift import DriftFinding, DriftLevel
from citadel_agent.upstream.layout import UpstreamLayout
from citadel_agent.upstream.sync import (
    DocDiff,
    DocFileChange,
    ParameterDiff,
    SyncBlockedError,
    SyncError,
    SyncReport,
    Verdict,
    apply_sync,
    diff_docs,
    diff_parameter_surfaces,
    diff_parameters,
    diff_tracks,
    evaluate,
    render_markdown,
    run_sync,
    summary_rows,
)

runner = CliRunner()

OLD = "1" * 40
NEW = "2" * 40
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
RELEASE = {
    "master-version": "1.0.2",
    "backend-contract-version": "1.1.0",
    "access-contract-version": "1.2.0",
    "publish-contract-version": "1.1.0-preview",
}


# Pure logic ------------------------------------------------------------------------------------


def _levels(old: dict[str, str], new: dict[str, str]) -> dict[str, CompatLevel]:
    return {t.track: t.level for t in diff_tracks(old, new)}


def test_identical_tracks_are_ok() -> None:
    changes = diff_tracks(RELEASE, RELEASE)
    assert {t.level for t in changes} == {CompatLevel.OK}
    assert not any(t.changed for t in changes)


def test_minor_and_patch_bumps_warn_but_major_blocks() -> None:
    new = {**RELEASE, "master-version": "1.0.3", "access-contract-version": "1.3.0"}
    levels = _levels(RELEASE, new)
    assert levels["master-version"] is CompatLevel.WARN
    assert levels["access-contract-version"] is CompatLevel.WARN
    assert levels["backend-contract-version"] is CompatLevel.OK

    major = _levels(RELEASE, {**RELEASE, "access-contract-version": "2.0.0"})
    assert major["access-contract-version"] is CompatLevel.BLOCK_WRITES


def test_backend_and_preview_tracks_require_an_exact_match() -> None:
    levels = _levels(
        RELEASE, {**RELEASE, "backend-contract-version": "1.1.1", "publish-contract-version": "1.2.0-preview"}
    )
    assert levels["backend-contract-version"] is CompatLevel.BLOCK_WRITES
    assert levels["publish-contract-version"] is CompatLevel.BLOCK_WRITES


def test_gateway_upgrade_preview_requires_an_exact_match() -> None:
    old = {**RELEASE, "gateway-upgrade-version": "1.0.0-preview"}
    assert _levels(old, {**old, "gateway-upgrade-version": "1.0.1-preview"})["gateway-upgrade-version"] is (
        CompatLevel.BLOCK_WRITES
    )


def test_removed_track_blocks_and_new_track_warns() -> None:
    new = {k: v for k, v in RELEASE.items() if k != "master-version"} | {"brand-new-version": "1.0.0"}
    changes = {t.track: t for t in diff_tracks(RELEASE, new)}
    assert changes["master-version"].level is CompatLevel.BLOCK_WRITES
    assert changes["master-version"].new is None
    assert changes["brand-new-version"].level is CompatLevel.WARN
    assert changes["brand-new-version"].old is None


OLD_PARAMS = {
    "apim": {"type": "object"},
    "mode": {"type": "string", "defaultValue": "a"},
    "count": {"type": "int", "defaultValue": 1},
    "gone": {"type": "string"},
    "flag": {"type": "bool", "defaultValue": False},
}
NEW_PARAMS = {
    "apim": {"type": "object"},
    "mode": {"type": "string"},
    "count": {"type": "string", "defaultValue": "1"},
    "extra": {"type": "array", "defaultValue": []},
    "must": {"type": "securestring"},
    "flag": {"type": "bool", "defaultValue": False},
}


def test_parameter_diff_reports_added_removed_and_changed() -> None:
    diff = diff_parameters("access", OLD_PARAMS, NEW_PARAMS)
    assert diff.template == "access"
    assert diff.added == ("extra (array, optional)", "must (securestring, required)")
    assert diff.removed == ("gone",)
    assert diff.changed == ("count: type int -> string", "mode: optional -> required")
    assert not diff.empty


def test_parameter_diff_of_identical_surfaces_is_empty() -> None:
    assert diff_parameters("alerts", OLD_PARAMS, OLD_PARAMS).empty


def test_parameter_surfaces_are_compared_per_template() -> None:
    old = {"access": OLD_PARAMS, "backend": OLD_PARAMS}
    new = {"access": OLD_PARAMS, "backend": NEW_PARAMS}
    diffs = {d.template: d for d in diff_parameter_surfaces(old, new)}
    assert diffs["access"].empty
    assert diffs["backend"].removed == ("gone",)


def test_doc_diff_counts_headings_per_file() -> None:
    old = parse_markdown("a.md", "# A\n## Keep\ntext\n## Dropped\ntext\n") + parse_markdown(
        "gone.md", "# Gone\n## Section\n"
    )
    new = parse_markdown(
        "a.md", "# A\n## Keep\ntext\n## Fresh\n```\n# not a heading\n```\n"
    ) + parse_markdown("b.md", "# B\n## One\n## Two\n")
    diff = diff_docs(old, new)

    assert diff.files_added == ("b.md",)
    assert diff.files_removed == ("gone.md",)
    by_path = {f.path: f for f in diff.files}
    assert by_path["a.md"].added == ("Fresh",)
    assert by_path["a.md"].removed == ("Dropped",)
    assert by_path["b.md"].added == ("B", "One", "Two")
    assert by_path["gone.md"].removed == ("Gone", "Section")
    assert (diff.added_count, diff.removed_count) == (4, 3)


def test_doc_diff_of_identical_documents_is_empty() -> None:
    sections = parse_markdown("a.md", "# A\n## B\n")
    assert diff_docs(sections, sections) == DocDiff()


def _report(**overrides: object) -> SyncReport:
    base = SyncReport(
        repository="https://example.test/repo.git",
        branch="citadel-v1",
        old_commit=OLD,
        new_commit=NEW,
        tracks=tuple(diff_tracks(RELEASE, RELEASE)),
        parameters=(ParameterDiff("access"), ParameterDiff("backend")),
        parameters_skipped=None,
        docs=DocDiff(),
        catalog_findings=(),
        drift_findings=(),
        generated_at=NOW,
    )
    return replace(base, **overrides)


def test_verdict_safe_without_findings() -> None:
    assert evaluate(_report()) == (Verdict.SAFE, [])


@pytest.mark.parametrize(
    ("overrides", "expected", "fragment"),
    [
        (
            {"tracks": tuple(diff_tracks(RELEASE, {**RELEASE, "master-version": "1.0.3"}))},
            Verdict.REVIEW,
            "master-version",
        ),
        (
            {"parameters": (ParameterDiff("access", added=("x (string, optional)",)),)},
            Verdict.REVIEW,
            "access template",
        ),
        ({"parameters_skipped": "no bicep"}, Verdict.REVIEW, "not checked"),
        (
            {"docs": DocDiff(files=(DocFileChange("a.md", removed=("Old",)),))},
            Verdict.REVIEW,
            "lost 1 section",
        ),
        ({"docs": DocDiff(files=(DocFileChange("a.md", added=("New",)),))}, Verdict.SAFE, None),
        (
            {
                "drift_findings": (
                    DriftFinding(DriftLevel.WARNING, "parameters", "access", "new optional parameter"),
                )
            },
            Verdict.REVIEW,
            "new optional parameter",
        ),
        (
            {
                "drift_findings": (
                    DriftFinding(DriftLevel.ERROR, "parameters", "access", "'x' no longer exists"),
                )
            },
            Verdict.BLOCKED,
            "no longer exists",
        ),
        ({"catalog_findings": ("errors/x: file gone",)}, Verdict.BLOCKED, "errors/x"),
        (
            {"tracks": tuple(diff_tracks(RELEASE, {**RELEASE, "publish-contract-version": "1.2.0-preview"}))},
            Verdict.BLOCKED,
            "publish-contract-version",
        ),
    ],
)
def test_verdict_rules(overrides: dict[str, object], expected: Verdict, fragment: str | None) -> None:
    verdict, reasons = evaluate(_report(**overrides))
    assert verdict is expected
    if fragment:
        assert any(fragment.lower() in reason.lower() for reason in reasons)


def test_blocked_outranks_review_findings() -> None:
    report = _report(parameters_skipped="no bicep", catalog_findings=("errors/x: gone",))
    verdict, reasons = evaluate(report)
    assert verdict is Verdict.BLOCKED
    assert reasons[0].startswith("Agent catalog")
    assert len(reasons) == 2


def test_report_properties() -> None:
    report = _report(new_commit=OLD)
    assert report.unchanged
    assert report.compat is CompatLevel.OK
    assert report.new_release == RELEASE
    assert report.verdict is Verdict.SAFE


def test_markdown_for_a_blocked_report() -> None:
    new_release = {**RELEASE, "publish-contract-version": "1.2.0-preview"}
    report = _report(
        tracks=tuple(diff_tracks(RELEASE, new_release)),
        parameters=(
            ParameterDiff("access", added=("extra (array, optional)",), removed=("gone",), changed=("a: b",)),
            ParameterDiff("backend"),
        ),
        docs=DocDiff(
            files=(
                DocFileChange(
                    "docs/a.md", added=tuple(f"Added {i}" for i in range(7)), removed=("Old | pipe",)
                ),
            ),
            files_added=("docs/new.md",),
        ),
        catalog_findings=("errors/jwt: file x no longer exists upstream",),
        drift_findings=(
            DriftFinding(DriftLevel.ERROR, "properties", "publish.backend", "renders unread keys"),
        ),
    )
    text = render_markdown(report)

    assert text.startswith(f"<!-- upstream-sync: verdict=BLOCKED old={OLD} new={NEW} -->\n")
    assert "**Verdict:** BLOCKED" in text
    assert "| Old commit | `1111111` |" in text
    assert "| New commit | `2222222` |" in text
    assert "| publish-contract-version | 1.1.0-preview | 1.2.0-preview | block-writes |" in text
    assert "| master-version | 1.0.2 | 1.0.2 | ok |" in text
    assert "### access" in text
    assert "- **Added:** `extra (array, optional)`" in text
    assert "- **Removed:** `gone`" in text
    assert "### backend\nNo changes." in text
    assert "**7** section(s) added, **1** removed across 1 file(s); 1 file(s) added, 0 removed." in text
    assert (
        "| docs/a.md | Added 0; Added 1; Added 2; Added 3; Added 4; ... and 2 more | Old \\| pipe |" in text
    )
    assert "| errors | errors/jwt | file x no longer exists upstream |" in text
    assert "| error | properties | publish.backend | renders unread keys |" in text
    assert "## Next steps" in text
    assert "Do **not** move the pin" in text
    assert text.endswith("\n")


def test_markdown_for_safe_and_review_next_steps() -> None:
    safe = render_markdown(_report())
    assert "**Verdict:** SAFE" in safe
    assert "No catalog entry references content that vanished upstream." in safe
    assert "No drift between the agent and the new upstream commit." in safe
    assert "upstream sync --apply" in safe
    assert "Read the findings" not in safe

    review = render_markdown(_report(parameters_skipped="no bicep"))
    assert "**Verdict:** REVIEW" in review
    assert "_Not checked: no bicep_" in review
    assert "1. Read the findings above" in review

    unchanged = render_markdown(_report(new_commit=OLD))
    assert "already the requested upstream commit" in unchanged
    assert "Nothing to do" in unchanged


def test_markdown_caps_long_lists() -> None:
    findings = tuple(f"errors/e{i}: gone" for i in range(40))
    text = render_markdown(_report(catalog_findings=findings))
    assert "... and 15 more." in text
    assert "errors/e24" in text
    assert "errors/e25" not in text


def test_summary_rows() -> None:
    rows = dict(summary_rows(_report(catalog_findings=("errors/x: gone",))))
    assert rows["Verdict"] == "BLOCKED"
    assert rows["Old commit"] == "1111111"
    assert rows["New commit"] == "2222222"
    assert rows["Catalog findings"] == "1"
    assert rows["Release tracks changed"].startswith("0 of 4")


# Orchestration with a fake git -----------------------------------------------------------------

OLD_TREE = {
    "release.json": json.dumps(RELEASE),
    "docs/guide.md": "# Guide\n## Setup\ntext\n",
    "params.json": json.dumps({"access": {"a": {"type": "string"}}}),
}


@dataclass
class FakeGit:
    """In-memory stand-in for ``sync.git``: serves ``git archive`` zips for known commits."""

    trees: dict[str, dict[str, str]]
    refs: dict[str, str]
    head: str = OLD
    fetched: str = ""
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, cwd: Path, *args: str) -> str:
        self.calls.append(args)
        command = args[0]
        if command == "fetch":
            if args[-1] not in self.refs:
                raise CommandError(f"fatal: couldn't find remote ref {args[-1]}")
            self.fetched = self.refs[args[-1]]
        elif command == "rev-parse":
            return self.fetched if args[1].startswith("FETCH_HEAD") else self.head
        elif command == "archive":
            target = Path(args[args.index("-o") + 1])
            with zipfile.ZipFile(target, "w") as bundle:
                for name, content in self.trees[args[-1]].items():
                    bundle.writestr(name, content)
        elif command == "checkout":
            self.head = args[-1]
        else:
            raise AssertionError(f"unexpected git command: {args}")
        return ""

    def mutating(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == "checkout"]


@dataclass
class Upstream:
    root: Path
    git: FakeGit
    drift: list[DriftFinding] = field(default_factory=list)
    catalogs: list[str] = field(default_factory=list)

    @property
    def lock_text(self) -> str:
        return (self.root / "citadel.lock.json").read_text(encoding="utf-8")


def _fake_compile(layout: UpstreamLayout) -> tuple[dict | None, str | None]:
    return json.loads((layout.root / "params.json").read_text(encoding="utf-8")), None


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Upstream:
    """A checkout locked to OLD whose upstream tip (NEW) only adds a documentation section."""
    root = tmp_path / "checkout"
    (root / "upstream" / "ai-hub-gateway").mkdir(parents=True)
    (root / "upstream" / "ai-hub-gateway" / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    write_lock(
        root,
        UpstreamLock(
            upstream=UpstreamPin(
                repository="https://example.test/repo.git",
                branch="citadel-v1",
                commit=OLD,
                path="upstream/ai-hub-gateway",
                release=RELEASE,
            ),
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    new_tree = {**OLD_TREE, "docs/guide.md": OLD_TREE["docs/guide.md"] + "## Extras\nmore\n"}
    fake = FakeGit(trees={OLD: OLD_TREE, NEW: new_tree}, refs={"citadel-v1": NEW, NEW: NEW, OLD: OLD})
    env = Upstream(root=root, git=fake)
    monkeypatch.setattr(sync, "git", fake)
    monkeypatch.setattr(sync, "_compile_surfaces", _fake_compile)
    monkeypatch.setattr(sync, "_drift_findings", lambda layout, surfaces: list(env.drift))
    monkeypatch.setattr(sync, "_catalog_findings", lambda root, index: list(env.catalogs))
    monkeypatch.setenv(paths.ENV_HOME, str(root))
    return env


def _set_new_tree(env: Upstream, **files: str) -> None:
    env.git.trees[NEW] = {**env.git.trees[NEW], **files}


def _run(env: Upstream, **kwargs: object) -> SyncReport:
    return run_sync(env.root, now=NOW, scratch_dir=env.root.parent, **kwargs)  # type: ignore[arg-type]


def test_run_sync_diffs_the_tip_of_the_locked_branch(upstream: Upstream) -> None:
    report = _run(upstream)

    assert (report.old_commit, report.new_commit) == (OLD, NEW)
    assert report.repository == "https://example.test/repo.git"
    assert report.verdict is Verdict.SAFE
    assert report.docs.files == (DocFileChange("docs/guide.md", added=("Extras",)),)
    assert all(d.empty for d in report.parameters)
    assert ("fetch", "--depth", "1", "origin", "citadel-v1") in upstream.git.calls
    assert upstream.git.mutating() == []


def test_run_sync_accepts_an_explicit_ref(upstream: Upstream) -> None:
    assert _run(upstream, ref=OLD).unchanged
    assert ("fetch", "--depth", "1", "origin", OLD) in upstream.git.calls


def test_run_sync_reports_every_kind_of_change(upstream: Upstream) -> None:
    _set_new_tree(
        upstream,
        **{
            "release.json": json.dumps({**RELEASE, "master-version": "1.0.3"}),
            "params.json": json.dumps({"access": {"a": {"type": "string"}, "b": {"type": "bool"}}}),
        },
    )
    upstream.drift = [DriftFinding(DriftLevel.WARNING, "parameters", "access", "new optional parameter 'x'")]
    report = _run(upstream)

    assert report.verdict is Verdict.REVIEW
    assert report.new_release["master-version"] == "1.0.3"
    access = next(d for d in report.parameters if d.template == "access")
    assert access.added == ("b (bool, required)",)


def test_run_sync_blocks_on_incompatible_release_catalogs_and_drift(upstream: Upstream) -> None:
    _set_new_tree(upstream, **{"release.json": json.dumps({**RELEASE, "access-contract-version": "2.0.0"})})
    upstream.catalogs = ["errors/jwt: x no longer exists"]
    upstream.drift = [DriftFinding(DriftLevel.ERROR, "parameters", "access", "boom")]
    report = _run(upstream)

    assert report.verdict is Verdict.BLOCKED
    assert report.compat is CompatLevel.BLOCK_WRITES
    assert report.catalog_findings == ("errors/jwt: x no longer exists",)


def test_unreadable_release_json_blocks(upstream: Upstream) -> None:
    _set_new_tree(upstream, **{"release.json": "not json"})
    report = _run(upstream)

    assert report.verdict is Verdict.BLOCKED
    assert any(f.area == "release" for f in report.drift_findings)


def test_skipped_parameter_surface_needs_review(upstream: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "_compile_surfaces", lambda layout: (None, "no bicep"))
    report = _run(upstream)

    assert report.parameters_skipped == "no bicep"
    assert report.parameters == ()
    assert report.verdict is Verdict.REVIEW


def test_run_sync_cleans_up_scratch_trees(upstream: Upstream) -> None:
    _run(upstream)
    assert not [p for p in upstream.root.parent.iterdir() if p.name.startswith("citadel-sync-")]


def test_run_sync_rejects_option_like_and_unknown_refs(upstream: Upstream) -> None:
    with pytest.raises(SyncError, match="Invalid ref"):
        _run(upstream, ref="--upload-pack=evil")
    with pytest.raises(SyncError, match="fetch failed"):
        _run(upstream, ref="no-such-branch")


def test_run_sync_requires_lock_and_initialized_submodule(upstream: Upstream) -> None:
    (upstream.root / "upstream" / "ai-hub-gateway" / ".git").unlink()
    with pytest.raises(SyncError, match="not initialized"):
        _run(upstream)
    (upstream.root / "citadel.lock.json").unlink()
    with pytest.raises(SyncError, match=r"citadel\.lock\.json"):
        _run(upstream)


def test_apply_moves_submodule_and_rewrites_lock(upstream: Upstream) -> None:
    _set_new_tree(upstream, **{"release.json": json.dumps({**RELEASE, "master-version": "1.0.3"})})
    report = _run(upstream)
    assert report.verdict is Verdict.REVIEW

    updated = apply_sync(upstream.root, report, now=NOW)

    assert upstream.git.mutating() == [("checkout", "--detach", NEW)]
    lock = read_lock(upstream.root)
    assert lock == updated
    assert lock.upstream.commit == NEW
    assert lock.upstream.release["master-version"] == "1.0.3"
    assert lock.upstream.repository == "https://example.test/repo.git"
    assert lock.synced_at == NOW


def test_apply_refuses_a_blocked_report(upstream: Upstream) -> None:
    upstream.catalogs = ["errors/x: gone"]
    before = upstream.lock_text
    with pytest.raises(SyncBlockedError):
        apply_sync(upstream.root, _run(upstream))

    assert upstream.git.mutating() == []
    assert upstream.lock_text == before


def test_apply_is_a_no_op_when_up_to_date(upstream: Upstream) -> None:
    before = upstream.lock_text
    apply_sync(upstream.root, _run(upstream, ref=OLD))

    assert upstream.git.mutating() == []
    assert upstream.lock_text == before


def test_apply_leaves_the_lock_alone_when_the_checkout_does_not_land(
    upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run(upstream)

    def stuck(cwd: Path, *args: str) -> str:
        return OLD if args[0] == "rev-parse" else ""

    monkeypatch.setattr(sync, "git", stuck)
    before = upstream.lock_text
    with pytest.raises(SyncError, match="lock not updated"):
        apply_sync(upstream.root, report)
    assert upstream.lock_text == before


# CLI -------------------------------------------------------------------------------------------


def test_dry_run_changes_nothing_and_writes_no_report(upstream: Upstream) -> None:
    before = upstream.lock_text
    result = runner.invoke(app, ["upstream", "sync"])

    assert result.exit_code == 0, result.output
    assert "SAFE" in result.output
    assert "Dry run" in result.output
    assert upstream.lock_text == before
    assert upstream.git.mutating() == []
    assert not (upstream.root / "UPSTREAM-COMPAT.md").exists()


def test_report_option_writes_markdown(upstream: Upstream, tmp_path: Path) -> None:
    target = tmp_path / "out" / "UPSTREAM-COMPAT.md"
    result = runner.invoke(app, ["upstream", "sync", "--report", str(target)])

    assert result.exit_code == 0, result.output
    text = target.read_text(encoding="utf-8")
    assert "**Verdict:** SAFE" in text
    assert "\r" not in text
    assert upstream.git.mutating() == []


@pytest.mark.parametrize(
    ("setup", "verdict", "exit_code"),
    [
        ("safe", "SAFE", 0),
        ("review", "REVIEW", 0),
        ("blocked", "BLOCKED", 1),
    ],
)
def test_check_exit_codes(upstream: Upstream, setup: str, verdict: str, exit_code: int) -> None:
    if setup == "review":
        _set_new_tree(upstream, **{"release.json": json.dumps({**RELEASE, "master-version": "1.0.3"})})
    if setup == "blocked":
        upstream.drift = [DriftFinding(DriftLevel.ERROR, "parameters", "access", "boom")]
    before = upstream.lock_text

    result = runner.invoke(app, ["upstream", "sync", "--check"])

    assert result.exit_code == exit_code, result.output
    assert verdict in result.output
    assert upstream.lock_text == before
    assert upstream.git.mutating() == []


def test_dry_run_of_a_blocked_commit_still_exits_zero(upstream: Upstream) -> None:
    upstream.catalogs = ["errors/x: gone"]
    assert runner.invoke(app, ["upstream", "sync"]).exit_code == 0


def test_apply_refuses_when_blocked_but_still_writes_the_report(upstream: Upstream, tmp_path: Path) -> None:
    _set_new_tree(upstream, **{"release.json": json.dumps({**RELEASE, "access-contract-version": "2.0.0"})})
    before = upstream.lock_text
    target = tmp_path / "report.md"

    result = runner.invoke(app, ["upstream", "sync", "--apply", "--report", str(target)])

    assert result.exit_code == 1, result.output
    assert "Refusing to apply" in result.output
    assert upstream.lock_text == before
    assert upstream.git.mutating() == []
    assert "**Verdict:** BLOCKED" in target.read_text(encoding="utf-8")


def test_apply_updates_lock_and_submodule(upstream: Upstream) -> None:
    result = runner.invoke(app, ["upstream", "sync", "--apply"])

    assert result.exit_code == 0, result.output
    assert upstream.git.mutating() == [("checkout", "--detach", NEW)]
    lock = read_lock(upstream.root)
    assert lock.upstream.commit == NEW
    assert lock.upstream.release == RELEASE
    assert lock.synced_at > datetime(2026, 1, 1, tzinfo=UTC)


def test_apply_with_a_ref_moves_to_that_commit(upstream: Upstream) -> None:
    result = runner.invoke(app, ["upstream", "sync", "--apply", "--ref", NEW])

    assert result.exit_code == 0, result.output
    assert read_lock(upstream.root).upstream.commit == NEW


def test_apply_when_up_to_date_changes_nothing(upstream: Upstream) -> None:
    before = upstream.lock_text
    result = runner.invoke(app, ["upstream", "sync", "--apply", "--ref", OLD])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
    assert upstream.lock_text == before
    assert upstream.git.mutating() == []


def test_unknown_ref_is_a_configuration_error(upstream: Upstream) -> None:
    result = runner.invoke(app, ["upstream", "sync", "--ref", "nope"])
    assert result.exit_code == 2
    assert "fetch failed" in result.output


def test_missing_lock_is_a_configuration_error(upstream: Upstream) -> None:
    (upstream.root / "citadel.lock.json").unlink()
    (upstream.root / "pyproject.toml").write_text("", encoding="utf-8")
    result = runner.invoke(app, ["upstream", "sync", "--check"])
    assert result.exit_code == 2


def test_missing_checkout_is_a_configuration_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "missing"))
    assert runner.invoke(app, ["upstream", "sync"]).exit_code == 2
