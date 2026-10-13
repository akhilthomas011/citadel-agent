# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Move the pinned upstream Citadel commit forward safely and reviewably.

``citadel-agent upstream sync`` compares the commit recorded in ``citadel.lock.json`` with a newer
upstream commit (by default the tip of the locked branch) without touching the checked-out
submodule: both trees are exported with ``git archive`` into a scratch directory. It then diffs the
release tracks, the parameter surfaces of the contract templates and the documentation sections,
runs the drift checks against the new commit, and rates the result :class:`Verdict.SAFE`,
:class:`Verdict.REVIEW` or :class:`Verdict.BLOCKED`. Only :func:`apply_sync` changes anything: it
moves the submodule and rewrites the lock, and only when the verdict is not blocked.

The diff functions and :func:`render_markdown` are pure. All git access goes through :func:`git`.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from citadel_agent.azure_clients.bicep import BicepCompileError
from citadel_agent.knowledge.docs import DocIndex, DocSection, build_doc_index
from citadel_agent.proc import CommandError, run_command
from citadel_agent.upstream.compat import CompatLevel, assess_release
from citadel_agent.upstream.drift import (
    DriftFinding,
    DriftLevel,
    check_parameter_surface,
    check_property_usage,
    filter_known,
    template_parameters,
)
from citadel_agent.upstream.layout import UpstreamLayout
from citadel_agent.upstream.lock import UpstreamLock, UpstreamPin, read_lock, read_release, write_lock

REPORT_FILE_NAME = "UPSTREAM-COMPAT.md"
GIT_TIMEOUT_SECONDS = 300
MAX_NAMES_PER_FILE = 5
MAX_FILES_LISTED = 20
MAX_ROWS_LISTED = 25

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./@-]*$")

ParameterSurface = Mapping[str, Mapping[str, Mapping[str, object]]]


class SyncError(RuntimeError):
    """The sync cannot run (missing lock or submodule, unknown ref, git failure): a configuration error."""


class SyncBlockedError(SyncError):
    """``--apply`` was refused because the new upstream commit is not compatible."""


class Verdict(StrEnum):
    SAFE = "SAFE"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class TrackChange:
    """One ``release.json`` track: the version the agent was tested with and the new upstream one."""

    track: str
    old: str | None
    new: str | None
    level: CompatLevel
    reason: str

    @property
    def changed(self) -> bool:
        return self.old != self.new


@dataclass(frozen=True)
class ParameterDiff:
    """Parameter surface changes of one contract template."""

    template: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


@dataclass(frozen=True)
class DocFileChange:
    """Headings added to and removed from one Markdown file."""

    path: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocDiff:
    files: tuple[DocFileChange, ...] = ()
    files_added: tuple[str, ...] = ()
    files_removed: tuple[str, ...] = ()

    @property
    def added_count(self) -> int:
        return sum(len(f.added) for f in self.files)

    @property
    def removed_count(self) -> int:
        return sum(len(f.removed) for f in self.files)


@dataclass(frozen=True)
class SyncReport:
    """Everything known about moving from ``old_commit`` to ``new_commit``."""

    repository: str
    branch: str
    old_commit: str
    new_commit: str
    tracks: tuple[TrackChange, ...]
    parameters: tuple[ParameterDiff, ...]
    parameters_skipped: str | None
    docs: DocDiff
    catalog_findings: tuple[str, ...]
    drift_findings: tuple[DriftFinding, ...]
    generated_at: datetime

    @property
    def unchanged(self) -> bool:
        return self.old_commit == self.new_commit

    @property
    def compat(self) -> CompatLevel:
        return max((t.level for t in self.tracks), key=lambda level: level.rank, default=CompatLevel.OK)

    @property
    def verdict(self) -> Verdict:
        return evaluate(self)[0]

    @property
    def reasons(self) -> list[str]:
        return evaluate(self)[1]

    @property
    def new_release(self) -> dict[str, str]:
        return {t.track: t.new for t in self.tracks if t.new is not None}


# Pure logic -----------------------------------------------------------------------------------


def diff_tracks(old: Mapping[str, str], new: Mapping[str, str]) -> tuple[TrackChange, ...]:
    """Rate each release track of the new upstream against the versions the agent was tested with."""
    assessments = assess_release(new, old).assessments
    return tuple(
        TrackChange(a.track, old.get(a.track), new.get(a.track), a.level, a.reason)
        for a in assessments.values()
    )


def _describe(definition: Mapping[str, object]) -> tuple[str, bool]:
    return str(definition.get("type", "")).lower(), "defaultValue" not in definition


def diff_parameters(
    template: str, old: Mapping[str, Mapping[str, object]], new: Mapping[str, Mapping[str, object]]
) -> ParameterDiff:
    """Compare the compiled ARM ``parameters`` of one template at two commits."""
    added = tuple(
        f"{name} ({kind}, {'required' if required else 'optional'})"
        for name, (kind, required) in ((n, _describe(d)) for n, d in new.items() if n not in old)
    )
    removed = tuple(name for name in old if name not in new)
    changed: list[str] = []
    for name in old.keys() & new.keys():
        (old_type, old_required), (new_type, new_required) = _describe(old[name]), _describe(new[name])
        if old_type != new_type:
            changed.append(f"{name}: type {old_type} -> {new_type}")
        if old_required != new_required:
            direction = "optional -> required" if new_required else "required -> optional"
            changed.append(f"{name}: {direction}")
    return ParameterDiff(template, added, removed, tuple(sorted(changed)))


def diff_parameter_surfaces(old: ParameterSurface, new: ParameterSurface) -> tuple[ParameterDiff, ...]:
    """Compare the parameter surfaces of every contract template (keyed by contract kind)."""
    kinds = [*new, *(k for k in old if k not in new)]
    return tuple(diff_parameters(kind, old.get(kind, {}), new.get(kind, {})) for kind in kinds)


def _headings(sections: Iterable[DocSection]) -> dict[str, dict[str, str]]:
    by_file: dict[str, dict[str, str]] = {}
    for section in sections:
        by_file.setdefault(section.path, {})
        if section.level > 0:
            by_file[section.path][section.anchor] = section.heading
    return by_file


def diff_docs(old: Iterable[DocSection], new: Iterable[DocSection]) -> DocDiff:
    """Headings added and removed per Markdown file (a renamed heading counts as removed plus added)."""
    before, after = _headings(old), _headings(new)
    files: list[DocFileChange] = []
    for path in sorted(before.keys() | after.keys()):
        old_h, new_h = before.get(path, {}), after.get(path, {})
        added = tuple(h for anchor, h in new_h.items() if anchor not in old_h)
        removed = tuple(h for anchor, h in old_h.items() if anchor not in new_h)
        if added or removed:
            files.append(DocFileChange(path, added, removed))
    return DocDiff(
        files=tuple(files),
        files_added=tuple(sorted(after.keys() - before.keys())),
        files_removed=tuple(sorted(before.keys() - after.keys())),
    )


def evaluate(report: SyncReport) -> tuple[Verdict, list[str]]:
    """Rate a report: blocked findings win, then anything a human should look at, otherwise safe."""
    blockers: list[str] = []
    review: list[str] = []
    for track in report.tracks:
        change = f"{track.track}: {track.old or '-'} -> {track.new or '-'} ({track.reason})"
        if track.level is CompatLevel.BLOCK_WRITES:
            blockers.append(f"Release track incompatible, {change}")
        elif track.level is CompatLevel.WARN:
            review.append(f"Release track changed, {change}")
    for finding in report.drift_findings:
        text = f"Drift [{finding.area}/{finding.template}]: {finding.message}"
        (blockers if finding.level is DriftLevel.ERROR else review).append(text)
    blockers.extend(f"Agent catalog: {finding}" for finding in report.catalog_findings)
    if report.parameters_skipped:
        review.append(f"Parameter surface not checked: {report.parameters_skipped}")
    review.extend(
        f"Parameter surface of the {d.template} template changed" for d in report.parameters if not d.empty
    )
    if report.docs.removed_count or report.docs.files_removed:
        review.append(
            f"Documentation lost {report.docs.removed_count} section(s) and "
            f"{len(report.docs.files_removed)} file(s); citations may break"
        )
    if blockers:
        return Verdict.BLOCKED, blockers + review
    return (Verdict.REVIEW, review) if review else (Verdict.SAFE, [])


# Markdown -------------------------------------------------------------------------------------


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return lines


def _capped(items: list[str], limit: int) -> list[str]:
    return [*items[:limit], f"... and {len(items) - limit} more"] if len(items) > limit else items


def _names(items: tuple[str, ...]) -> str:
    return "; ".join(_capped(list(items), MAX_NAMES_PER_FILE)) or "-"


def _parameter_lines(report: SyncReport) -> list[str]:
    if report.parameters_skipped:
        return [f"_Not checked: {report.parameters_skipped}_"]
    lines: list[str] = []
    for diff in report.parameters:
        lines.append(f"### {diff.template}")
        if diff.empty:
            lines += ["No changes.", ""]
            continue
        for label, items in (("Added", diff.added), ("Removed", diff.removed), ("Changed", diff.changed)):
            if items:
                lines.append(f"- **{label}:** " + ", ".join(f"`{i}`" for i in items))
        lines.append("")
    return lines


def _doc_lines(docs: DocDiff) -> list[str]:
    if not (docs.files or docs.files_added or docs.files_removed):
        return ["No section changes."]
    lines = [
        f"**{docs.added_count}** section(s) added, **{docs.removed_count}** removed across "
        f"{len(docs.files)} file(s); {len(docs.files_added)} file(s) added, "
        f"{len(docs.files_removed)} removed.",
        "",
    ]
    rows = [[f.path, _names(f.added), _names(f.removed)] for f in docs.files[:MAX_FILES_LISTED]]
    lines += _table(["File", "Added sections", "Removed sections"], rows)
    if len(docs.files) > MAX_FILES_LISTED:
        lines += ["", f"... and {len(docs.files) - MAX_FILES_LISTED} more file(s)."]
    return lines


def _next_steps(report: SyncReport) -> list[str]:
    if report.unchanged:
        return ["Nothing to do: the pin is already at the requested upstream commit."]
    new = report.new_commit[:7]
    if report.verdict is Verdict.BLOCKED:
        return [
            "Do **not** move the pin automatically; `upstream sync --apply` refuses while the verdict "
            "is BLOCKED.",
            "Fix the agent for the findings above: contract models and `AGENT_PARAMETERS` in "
            "`upstream/drift.py`, the knowledge catalogs, and the tested release tracks.",
            f"Check out the new commit (`git -C upstream/ai-hub-gateway checkout {new}`), run the tests "
            "(`uv run pytest -q`, plus `uv run pytest -m az` with the Bicep CLI installed), then run "
            "`uv run citadel-agent upstream lock` to record it.",
        ]
    steps = [
        "Run `uv run citadel-agent upstream sync --apply` to move the submodule and rewrite "
        "`citadel.lock.json` (the scheduled workflow does this and opens a pull request).",
        "Run `uv run pytest -q` and, with the Bicep CLI installed, `uv run pytest -m az`.",
    ]
    if report.verdict is Verdict.REVIEW:
        steps.insert(0, "Read the findings above and update the agent where needed before merging.")
    return [*steps, "Commit the submodule pointer together with `citadel.lock.json`."]


def render_markdown(report: SyncReport) -> str:
    """Render the report as the Markdown document written to ``UPSTREAM-COMPAT.md``."""
    verdict = report.verdict
    lines = [
        f"<!-- upstream-sync: verdict={verdict} old={report.old_commit} new={report.new_commit} -->",
        "# Upstream compatibility report",
        "",
        f"**Verdict:** {verdict}",
        "",
        *_table(
            ["Property", "Value"],
            [
                ["Repository", report.repository],
                ["Branch", report.branch],
                ["Old commit", f"`{report.old_commit[:7]}`"],
                ["New commit", f"`{report.new_commit[:7]}`"],
                ["Generated", report.generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")],
            ],
        ),
        "",
    ]
    if report.unchanged:
        lines += ["The pinned commit is already the requested upstream commit.", ""]
    if report.reasons:
        lines += ["### Why", ""]
        lines += [f"- {reason}" for reason in _capped(report.reasons, MAX_ROWS_LISTED)]
        lines.append("")
    lines += [
        "## Release tracks",
        "",
        *_table(
            ["Track", "Old", "New", "Compatibility", "Notes"],
            [[t.track, t.old or "-", t.new or "-", t.level.value, t.reason] for t in report.tracks],
        ),
        "",
        "## Parameter surface",
        "",
        *_parameter_lines(report),
        "## Documentation",
        "",
        *_doc_lines(report.docs),
        "",
        "## Agent catalogs",
        "",
    ]
    if report.catalog_findings:
        rows = []
        for finding in report.catalog_findings[:MAX_ROWS_LISTED]:
            label, _, problem = finding.partition(": ")
            rows.append([label.split("/")[0], label, problem])
        lines += _table(["Catalog", "Entry", "Problem"], rows)
        if len(report.catalog_findings) > MAX_ROWS_LISTED:
            lines += ["", f"... and {len(report.catalog_findings) - MAX_ROWS_LISTED} more."]
    else:
        lines.append("No catalog entry references content that vanished upstream.")
    lines += ["", "## Drift checks", ""]
    if report.drift_findings:
        rows = [
            [f.level.value, f.area, f.template, f.message] for f in report.drift_findings[:MAX_ROWS_LISTED]
        ]
        lines += _table(["Level", "Area", "Template", "Message"], rows)
        if len(report.drift_findings) > MAX_ROWS_LISTED:
            lines += ["", f"... and {len(report.drift_findings) - MAX_ROWS_LISTED} more."]
    else:
        lines.append("No drift between the agent and the new upstream commit.")
    lines += ["", "## Next steps", ""]
    lines += [f"{number}. {step}" for number, step in enumerate(_next_steps(report), start=1)]
    return "\n".join(lines) + "\n"


def summary_rows(report: SyncReport) -> list[tuple[str, str]]:
    """Key facts of a report for the console summary."""
    changed = [t for t in report.tracks if t.changed]
    parameter_changes = sum(1 for d in report.parameters if not d.empty)
    return [
        ("Verdict", report.verdict.value),
        ("Old commit", report.old_commit[:7]),
        ("New commit", report.new_commit[:7]),
        (
            "Release tracks changed",
            f"{len(changed)} of {len(report.tracks)} (compatibility: {report.compat.value})",
        ),
        ("Parameter surface", report.parameters_skipped or f"{parameter_changes} template(s) changed"),
        ("Doc sections", f"+{report.docs.added_count} / -{report.docs.removed_count}"),
        ("Catalog findings", str(len(report.catalog_findings))),
        ("Drift findings", str(len(report.drift_findings))),
    ]


# Git and orchestration ------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    """Run an allowlisted ``git`` command and return its trimmed standard output (the only git access)."""
    return run_command("git", args, cwd=cwd, timeout=GIT_TIMEOUT_SECONDS).stdout.strip()


def _git(cwd: Path, *args: str) -> str:
    try:
        return git(cwd, *args)
    except CommandError as exc:
        raise SyncError(f"git {args[0]} failed: {exc}") from exc


def _load_lock(root: Path) -> UpstreamLock:
    try:
        return read_lock(root)
    except (FileNotFoundError, ValueError) as exc:
        raise SyncError(f"Could not read citadel.lock.json: {exc}") from exc


def resolve_target(submodule: Path, ref: str) -> str:
    """Fetch ``ref`` (a branch or commit) from the submodule's origin and return its full commit id."""
    if not _SAFE_REF.match(ref):
        raise SyncError(f"Invalid ref: {ref!r}")
    _git(submodule, "fetch", "--depth", "1", "origin", ref)
    commit = _git(submodule, "rev-parse", "FETCH_HEAD^{commit}")
    if not _COMMIT.match(commit):
        raise SyncError(f"Could not resolve {ref!r} to a commit (got {commit!r}).")
    return commit


def export_commit(submodule: Path, commit: str, destination: Path) -> Path:
    """Export the tree of ``commit`` into ``destination`` without touching the submodule checkout."""
    archive = destination.parent / f"{destination.name}.zip"
    _git(submodule, "archive", "--format=zip", "-o", str(archive), commit)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(destination)
    archive.unlink()
    return destination


def _compile_surfaces(
    layout: UpstreamLayout,
) -> tuple[dict[str, dict[str, dict[str, object]]] | None, str | None]:
    try:
        surfaces = {kind: template_parameters(path) for kind, path in layout.contract_templates().items()}
    except (BicepCompileError, CommandError) as exc:
        return None, f"could not compile the contract templates ({exc})"
    return surfaces, None


def _catalog_findings(upstream_root: Path, index: DocIndex) -> list[str]:
    from citadel_agent.knowledge import catalog_drift, load_catalogs

    return catalog_drift(load_catalogs(), upstream_root, index)


def _drift_findings(layout: UpstreamLayout, surfaces: ParameterSurface | None) -> list[DriftFinding]:
    findings = [
        DriftFinding(DriftLevel.ERROR, "layout", layout.relative(path), "required upstream file is missing")
        for path in layout.missing_files()
    ]
    findings += check_property_usage(layout)
    if surfaces is not None:
        findings += check_parameter_surface(layout, surfaces)
    return filter_known(findings)


def _read_release(layout: UpstreamLayout) -> tuple[dict[str, str], list[DriftFinding]]:
    try:
        return read_release(layout.root), []
    except (OSError, ValueError) as exc:
        finding = DriftFinding(DriftLevel.ERROR, "release", "release.json", f"cannot be read: {exc}")
        return {}, [finding]


def run_sync(
    root: Path,
    *,
    ref: str | None = None,
    now: datetime | None = None,
    scratch_dir: Path | None = None,
    compile_surfaces: Callable[[UpstreamLayout], tuple[dict | None, str | None]] | None = None,
) -> SyncReport:
    """Compare the locked upstream commit with ``ref`` (default: tip of the locked branch).

    Nothing in the checkout changes; the submodule's object store gains the fetched commit.

    Args:
        root: Agent checkout root.
        ref: Branch or commit to move to; defaults to the branch recorded in the lock.
        now: Timestamp for the report; defaults to the current time.
        scratch_dir: Parent directory for the exported trees; defaults to the system temp directory.
        compile_surfaces: Compiles the contract template parameters of an exported tree
            (returns ``(surfaces, skipped_reason)``); defaults to the Bicep CLI.

    Raises:
        SyncError: If the lock, the submodule or the ref cannot be used.
    """
    compile_fn = compile_surfaces or _compile_surfaces
    lock = _load_lock(root)
    submodule = root / lock.upstream.path
    if not (submodule / ".git").exists():
        raise SyncError(
            f"The upstream submodule is not initialized at {lock.upstream.path}. "
            "Run: git submodule update --init --depth 1"
        )
    new_commit = resolve_target(submodule, ref or lock.upstream.branch)
    old_commit = lock.upstream.commit

    with tempfile.TemporaryDirectory(
        prefix="citadel-sync-", dir=scratch_dir, ignore_cleanup_errors=True
    ) as tmp:
        scratch = Path(tmp)
        old_root = export_commit(submodule, old_commit, scratch / "old")
        new_root = (
            old_root if new_commit == old_commit else export_commit(submodule, new_commit, scratch / "new")
        )
        old_layout, new_layout = UpstreamLayout(old_root), UpstreamLayout(new_root)

        new_release, release_findings = _read_release(new_layout)
        old_surfaces, old_skipped = compile_fn(old_layout)
        if new_root == old_root:
            new_surfaces, new_skipped = old_surfaces, old_skipped
        else:
            new_surfaces, new_skipped = compile_fn(new_layout)
        skipped = new_skipped or old_skipped
        parameters = (
            diff_parameter_surfaces(old_surfaces, new_surfaces)
            if old_surfaces is not None and new_surfaces is not None
            else ()
        )

        old_index, new_index = build_doc_index(old_root), build_doc_index(new_root)
        return SyncReport(
            repository=lock.upstream.repository,
            branch=lock.upstream.branch,
            old_commit=old_commit,
            new_commit=new_commit,
            tracks=diff_tracks(lock.upstream.release, new_release),
            parameters=parameters,
            parameters_skipped=skipped,
            docs=diff_docs(old_index.sections, new_index.sections),
            catalog_findings=tuple(_catalog_findings(new_root, new_index)),
            drift_findings=tuple(release_findings + _drift_findings(new_layout, new_surfaces)),
            generated_at=now or datetime.now(UTC),
        )


def apply_sync(root: Path, report: SyncReport, *, now: datetime | None = None) -> UpstreamLock:
    """Move the submodule to ``report.new_commit`` and rewrite ``citadel.lock.json``.

    Raises:
        SyncBlockedError: If the report is BLOCKED; nothing is changed.
        SyncError: If the submodule cannot be moved; the lock is left untouched.
    """
    if report.verdict is Verdict.BLOCKED:
        raise SyncBlockedError("The new upstream commit is not compatible; refusing to move the pin.")
    lock = _load_lock(root)
    if report.unchanged:
        return lock
    submodule = root / lock.upstream.path
    _git(submodule, "checkout", "--detach", report.new_commit)
    head = _git(submodule, "rev-parse", "HEAD")
    if head != report.new_commit:
        raise SyncError(
            f"The submodule is at {head[:7]} instead of {report.new_commit[:7]}; lock not updated."
        )
    pin = UpstreamPin(
        **{**lock.upstream.model_dump(), "commit": report.new_commit, "release": report.new_release}
    )
    updated = UpstreamLock(upstream=pin, synced_at=now or datetime.now(UTC))
    write_lock(root, updated)
    return updated
