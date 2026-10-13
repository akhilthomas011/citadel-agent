# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Search the pinned upstream documentation.

The upstream Markdown files are split into sections at their headings and indexed with BM25. Each
hit is cited as ``path#anchor`` (GitHub anchors) with a link to the pinned upstream commit, so
answers always point at the exact documentation the agent was built against. Nothing is copied
from upstream: the index is built from the submodule at runtime.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_RAW_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SLUG_DROP = re.compile(r"[^\w\- ]", re.UNICODE)
_STOPWORD_TEXT = (
    "a an and are as at be by can do does for from how i if in into is it its my of on or "
    "that the their then there these this to use used using was we what when which will "
    "with you your"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
EXCLUDED_DIRS = frozenset({"node_modules", ".git", ".venv"})
SNIPPET_CHARS = 280


def tokenize(text: str) -> list[str]:
    """Lowercase tokens, keeping compound identifiers (``allowedModels``) and their parts."""
    tokens: list[str] = []
    for raw in _RAW_TOKEN.findall(text):
        raw = raw.strip("._-")
        if not raw:
            continue
        whole = raw.lower()
        parts = [p.lower() for piece in re.split(r"[-_.]", raw) for p in _CAMEL.split(piece) if p]
        for token in {whole, *parts} if len(parts) > 1 else {whole}:
            if len(token) > 1 and token not in _STOPWORDS:
                tokens.append(token)
    return tokens


def github_anchor(heading: str) -> str:
    """Approximate GitHub's heading anchor: rendered text, lowercased, punctuation removed."""
    text = _LINK.sub(r"\1", heading).replace("`", "")
    text = re.sub(r"(\*\*|__|\*)", "", text)
    return _SLUG_DROP.sub("", text.strip().lower()).replace(" ", "-")


@dataclass(frozen=True)
class DocSection:
    """One heading of an upstream Markdown file and the text up to the next heading."""

    path: str
    heading: str
    anchor: str
    level: int
    breadcrumb: tuple[str, ...]
    text: str

    @property
    def citation(self) -> str:
        return f"{self.path}#{self.anchor}" if self.anchor else self.path

    @property
    def title(self) -> str:
        return " > ".join((*self.breadcrumb, self.heading))


@dataclass(frozen=True)
class DocHit:
    section: DocSection
    score: float
    snippet: str
    url: str


def _strip_frontmatter(lines: list[str]) -> tuple[list[str], str | None]:
    if not lines or lines[0].strip() != "---":
        return lines, None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            title = next(
                (
                    ln.split(":", 1)[1].strip().strip("'\"")
                    for ln in lines[1:index]
                    if ln.startswith("title:")
                ),
                None,
            )
            return lines[index + 1 :], title
    return lines, None


def parse_markdown(path: str, text: str) -> list[DocSection]:
    """Split a Markdown document into sections (headings inside code fences are ignored)."""
    lines, title = _strip_frontmatter(text.splitlines())
    sections: list[DocSection] = []
    stack: list[tuple[int, str]] = []
    seen: Counter[str] = Counter()
    heading, anchor, level = title or Path(path).stem, "", 0
    breadcrumb: tuple[str, ...] = ()
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        content = "\n".join(body).strip()
        if content or level > 0:
            sections.append(DocSection(path, heading, anchor, level, breadcrumb, content))

    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            body.append(line)
            continue
        flush()
        level = len(match.group(1))
        heading = _LINK.sub(r"\1", match.group(2)).strip()
        base = github_anchor(match.group(2))
        anchor = base if not seen[base] else f"{base}-{seen[base]}"
        seen[base] += 1
        while stack and stack[-1][0] >= level:
            stack.pop()
        breadcrumb = tuple(h for _, h in stack)
        stack.append((level, heading))
        body = []
    flush()
    return sections


@dataclass
class DocIndex:
    """BM25 index over upstream documentation sections."""

    sections: list[DocSection]
    base_url: str | None = None
    k1: float = 1.5
    b: float = 0.75
    _postings: dict[str, dict[int, int]] = field(default_factory=dict, init=False, repr=False)
    _lengths: list[int] = field(default_factory=list, init=False, repr=False)
    _by_citation: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        for index, section in enumerate(self.sections):
            # Headings and their parents are weighted twice: they summarize the section.
            heading_tokens = tokenize(" ".join((*section.breadcrumb, section.heading)))
            counts = Counter(heading_tokens * 2 + tokenize(section.text))
            self._lengths.append(sum(counts.values()))
            for token, count in counts.items():
                self._postings.setdefault(token, {})[index] = count
            self._by_citation.setdefault(section.citation.lower(), index)
        self._average = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0

    @property
    def files(self) -> list[str]:
        return sorted({s.path for s in self.sections})

    def url(self, section: DocSection) -> str | None:
        if not self.base_url:
            return None
        return f"{self.base_url}/{section.path}" + (f"#{section.anchor}" if section.anchor else "")

    def search(self, query: str, *, limit: int = 5, path_prefix: str | None = None) -> list[DocHit]:
        """Return the best matching sections for ``query``."""
        terms = set(tokenize(query))
        if not terms or not self.sections:
            return []
        total = len(self.sections)
        scores: dict[int, float] = {}
        for term in terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = math.log(1 + (total - len(postings) + 0.5) / (len(postings) + 0.5))
            for index, frequency in postings.items():
                norm = self.k1 * (1 - self.b + self.b * self._lengths[index] / (self._average or 1))
                scores[index] = scores.get(index, 0.0) + idf * frequency * (self.k1 + 1) / (frequency + norm)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        hits: list[DocHit] = []
        for index, score in ranked:
            section = self.sections[index]
            if path_prefix and not section.path.startswith(path_prefix):
                continue
            hits.append(
                DocHit(section, round(score, 3), _snippet(section.text, terms), self.url(section) or "")
            )
            if len(hits) >= limit:
                break
        return hits

    def find(self, citation: str) -> DocSection | None:
        """Look a section up by ``path#anchor`` (case-insensitive)."""
        index = self._by_citation.get(citation.strip().lower())
        return self.sections[index] if index is not None else None

    def read(self, citation: str, *, max_chars: int = 8000) -> str | None:
        """Return a section with its subsections as Markdown (truncated to ``max_chars``)."""
        index = self._by_citation.get(citation.strip().lower())
        if index is None:
            return None
        start = self.sections[index]
        parts = [f"{'#' * max(start.level, 1)} {start.heading}", start.text]
        for section in self.sections[index + 1 :]:
            if section.path != start.path or (start.level and section.level <= start.level):
                break
            parts += [f"{'#' * section.level} {section.heading}", section.text]
        text = "\n\n".join(p for p in parts if p).strip()
        return text if len(text) <= max_chars else text[:max_chars] + "\n\n[truncated]"


def _snippet(text: str, terms: set[str]) -> str:
    flat = " ".join(text.split())
    lowered = flat.lower()
    positions = [lowered.find(term) for term in terms if term in lowered]
    start = max(0, min(positions) - SNIPPET_CHARS // 4) if positions else 0
    snippet = flat[start : start + SNIPPET_CHARS]
    return ("..." if start else "") + snippet + ("..." if start + SNIPPET_CHARS < len(flat) else "")


def iter_markdown_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.md")):
        if not EXCLUDED_DIRS.intersection(path.relative_to(root).parts):
            yield path


def build_doc_index(root: Path, *, base_url: str | None = None) -> DocIndex:
    """Index every Markdown file under ``root`` (the upstream submodule)."""
    sections: list[DocSection] = []
    for path in iter_markdown_files(root):
        relative = path.relative_to(root).as_posix()
        sections.extend(parse_markdown(relative, path.read_text(encoding="utf-8", errors="replace")))
    return DocIndex(sections, base_url=base_url)


def github_blob_url(repository: str, commit: str) -> str:
    """``https://github.com/org/repo/blob/<commit>`` for a repository clone URL."""
    return f"{repository.rstrip('/').removesuffix('.git')}/blob/{commit}"


@lru_cache(maxsize=4)
def cached_doc_index(root: str, repository: str, commit: str) -> DocIndex:
    """The doc index for one upstream commit (built once per process)."""
    return build_doc_index(Path(root), base_url=github_blob_url(repository, commit))
