"""
Docs regression tests for issue #2548 — context PR + per-slice BRC history.

Slice-1 of #2548 updates documentation files to reflect the new
runtime behavior that landed across earlier slices:

* `docs/architecture/orchestrator.md` — now describes the
  `pr.context_branch` / `pr.context_pr_number` contract fields and the
  per-slice BRC-history file naming pattern (``-implement-slice-<N>``).
* `docs/reference/orchestrator-cli.md` — surfaces any context-PR
  command/flag exposure and cross-references the new contract fields.
* `docs/guides/concurrent-execution.md` — adds an explicit
  "Context PR" subsection to the PR-stack diagram.

The risk this test guards against is *silent docs drift*: a future PR
restoring the deprecated terminology, or removing the new pinning
language, would be invisible to humans skimming a diff but would re-open
the gap that #2548 closed.

Each test pins one acceptance-criterion line to a literal-string check
so the failure message points directly at the missing element. The
deprecated-filename grep uses a directory-scoped scan with an explicit
allow-list of known-legitimate references (operational documentation
covering non-slice mode where the aggregate file is still emitted, plus
the file-tree reference in ``docs/guides/sdlc-pipeline.md``).

If the documenter moves a section heading or changes wording, this file
will fail loudly and direct the change to the planner: docs drift is
exactly what slice-1 exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_ROOT = PROJECT_ROOT / "docs"

ARCHITECTURE_ORCHESTRATOR = DOCS_ROOT / "architecture" / "orchestrator.md"
REFERENCE_ORCHESTRATOR_CLI = DOCS_ROOT / "reference" / "orchestrator-cli.md"
GUIDES_CONCURRENT_EXECUTION = DOCS_ROOT / "guides" / "concurrent-execution.md"


def _read(path: Path) -> str:
    assert path.exists(), f"Expected docs file at {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# task-1-1: docs/architecture/orchestrator.md pinning
# ---------------------------------------------------------------------------


class TestArchitectureOrchestratorContextFields:
    """`docs/architecture/orchestrator.md` must document the new
    `pr.context_*` contract fields and the per-slice BRC filename
    pattern. Acceptance criterion for task-1-1 (#2548)."""

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(ARCHITECTURE_ORCHESTRATOR)

    # NOTE: `pr.context_branch` mention assertion deleted in #2777 slice-2
    # (task-2-10). The ``pr.context_branch`` contract field was removed by
    # slice-2 task-2-4 (cq-2 hard-remove); the docs must no longer
    # reference it. The replacement regression test (asserting the
    # deleted-field mentions are *absent* from the architecture doc) lives
    # in :class:`TestArchitectureOrchestratorNoDeletedFieldMentions`
    # below.

    def test_mentions_pr_context_pr_number(self, text: str) -> None:
        assert "pr.context_pr_number" in text, (
            "task-1-1: docs/architecture/orchestrator.md must reference "
            "`pr.context_pr_number` (the surviving context-PR contract "
            "field — kept post-#2777 slice-2). Without this, readers "
            "cannot trace the PR number that the orchestrator stamps back "
            "onto the contract after opening the context PR. See #2548."
        )

    def test_references_per_slice_brc_filename_pattern(self, text: str) -> None:
        # The acceptance criterion calls out the per-slice filename
        # pattern. We accept either the bare suffix or any concrete
        # rendering of it (`{identifier}-implement-slice-<N>` etc.).
        assert "-implement-slice-" in text, (
            "task-1-1: docs/architecture/orchestrator.md must reference "
            "the per-slice BRC filename pattern (`-implement-slice-<N>`). "
            "Without this, the doc still implies a single aggregate "
            "implement file, which is no longer produced in slice-aware "
            "mode (#2548 hard switchover)."
        )

    def test_cross_references_issue_2548(self, text: str) -> None:
        # The contract requires each affected doc to cross-reference
        # #2548 so future readers can navigate to the originating issue.
        assert "#2548" in text, (
            "task-1-1: docs/architecture/orchestrator.md must "
            "cross-reference issue #2548 so the rationale is one click "
            "away. The contract task description requires this cross-ref."
        )


# Deleted PRMetadata field names (#2777 slice-2 task-2-4). The
# orchestrator and CLI docs MUST stop referencing these once the
# accompanying documenter task (slice-3 task-3-12) lands; the
# regression classes below pin that requirement.
_DELETED_PR_METADATA_FIELDS: tuple[str, ...] = (
    "pr.context_branch",
    "pr.context_title",
    "pr.context_description",
)


class TestArchitectureOrchestratorNoDeletedFieldMentions:
    """``docs/architecture/orchestrator.md`` must not reference the three
    ``PRMetadata`` fields deleted by #2777 slice-2.

    The mentions get removed by slice-3 task-3-12 (the documenter pass
    that updates docs for the context-PR topology collapse). Until that
    task lands, these checks ``xfail`` (``strict=False``) — they flip
    to ``XPASS`` once the docs are updated, and CI keeps passing in
    both modes.
    """

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(ARCHITECTURE_ORCHESTRATOR)

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Docs cleanup for the deleted PRMetadata fields is owned by "
            "#2777 slice-3 task-3-12 (documenter). This test exists in "
            "slice-2 so the regression test 'docs must not mention "
            "deleted fields' is committed atomically with the schema "
            "deletion. It flips to XPASS automatically when slice-3 "
            "lands."
        ),
    )
    @pytest.mark.parametrize("deleted_field", _DELETED_PR_METADATA_FIELDS)
    def test_no_mention_of_deleted_field(self, text: str, deleted_field: str) -> None:
        assert deleted_field not in text, (
            f"docs/architecture/orchestrator.md still references the "
            f"deleted PRMetadata field {deleted_field!r}. #2777 slice-2 "
            f"task-2-4 removed this field from the schema; the "
            f"accompanying doc update is owned by slice-3 task-3-12. "
            f"Update the doc to drop the field reference."
        )


class TestReferenceOrchestratorCliNoDeletedFieldMentions:
    """``docs/reference/orchestrator-cli.md`` must not reference the three
    deleted PRMetadata fields. Symmetric with the architecture-doc test
    above; both flip from XFAIL to XPASS when slice-3 task-3-12 lands.
    """

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(REFERENCE_ORCHESTRATOR_CLI)

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Docs cleanup tracked in #2777 slice-3 task-3-12. "
            "See TestArchitectureOrchestratorNoDeletedFieldMentions."
        ),
    )
    @pytest.mark.parametrize("deleted_field", _DELETED_PR_METADATA_FIELDS)
    def test_no_mention_of_deleted_field(self, text: str, deleted_field: str) -> None:
        assert deleted_field not in text, (
            f"docs/reference/orchestrator-cli.md still references the "
            f"deleted PRMetadata field {deleted_field!r}. #2777 slice-2 "
            f"task-2-4 removed this field; the doc update is owned by "
            f"slice-3 task-3-12."
        )


class TestConcurrentExecutionNoDeletedFieldMentions:
    """``docs/guides/concurrent-execution.md`` must not reference the three
    deleted PRMetadata fields. Same XFAIL → XPASS pattern as siblings.
    """

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(GUIDES_CONCURRENT_EXECUTION)

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "Docs cleanup tracked in #2777 slice-3 task-3-12. "
            "See TestArchitectureOrchestratorNoDeletedFieldMentions."
        ),
    )
    @pytest.mark.parametrize("deleted_field", _DELETED_PR_METADATA_FIELDS)
    def test_no_mention_of_deleted_field(self, text: str, deleted_field: str) -> None:
        assert deleted_field not in text, (
            f"docs/guides/concurrent-execution.md still references the "
            f"deleted PRMetadata field {deleted_field!r}. #2777 slice-2 "
            f"task-2-4 removed this field; the doc update is owned by "
            f"slice-3 task-3-12."
        )


class TestArchitectureOrchestratorNoDeprecatedReferences:
    """The architecture doc must not still describe the aggregate
    ``{identifier}-implement.md`` / ``.json`` file as the canonical
    implement-phase BRC file — it is no longer produced in slice mode.
    """

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(ARCHITECTURE_ORCHESTRATOR)

    def test_no_deprecated_aggregate_filename(self, text: str) -> None:
        # The acceptance criterion says: "Search for the literal string
        # `<id>-implement.json` (or `.md`) in these two files: zero
        # remaining matches outside changelog/historical references."
        # We search for the deprecated literal and require that any hit
        # appears in an explicit `historical:` / `was:` / `legacy`
        # framing (an allow-list of known explanatory contexts).
        offending = _find_deprecated_filename_lines(text)
        assert not offending, (
            "task-1-1: docs/architecture/orchestrator.md still references "
            "the deprecated aggregate filename pattern "
            "`{identifier}-implement.{md,json}` outside an allow-listed "
            "historical context. Update each line to the per-slice "
            "filename `{identifier}-implement-slice-<N>.{md,json}` or "
            "wrap it in an explicit historical/legacy framing."
            f" Offending lines:\n{_format_lines(offending)}"
        )


# ---------------------------------------------------------------------------
# task-1-1: docs/reference/orchestrator-cli.md pinning
# ---------------------------------------------------------------------------


class TestReferenceOrchestratorCliContextFields:
    """`docs/reference/orchestrator-cli.md` cross-references the new
    `pr.context_*` contract fields. Acceptance criterion for task-1-1
    (#2548)."""

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(REFERENCE_ORCHESTRATOR_CLI)

    def test_mentions_pr_context_pr_number(self, text: str) -> None:
        # ``pr.context_branch`` was deleted by #2777 slice-2 task-2-4.
        # Only ``pr.context_pr_number`` survives, and the CLI doc must
        # still surface it so CLI users can locate the open context PR
        # via ``gh pr view``.
        has_pr_num = "pr.context_pr_number" in text or "context_pr_number" in text
        assert has_pr_num, (
            "docs/reference/orchestrator-cli.md must reference "
            "`pr.context_pr_number` so CLI users can trace the surviving "
            "context-PR contract field. See #2548 and #2777 slice-2."
        )

    def test_cross_references_issue_2548(self, text: str) -> None:
        assert "#2548" in text, (
            "task-1-1: docs/reference/orchestrator-cli.md must "
            "cross-reference issue #2548 so the rationale is one click "
            "away. The contract task description requires this cross-ref."
        )


class TestReferenceOrchestratorCliNoDeprecatedReferences:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(REFERENCE_ORCHESTRATOR_CLI)

    def test_no_deprecated_aggregate_filename(self, text: str) -> None:
        offending = _find_deprecated_filename_lines(text)
        assert not offending, (
            "task-1-1: docs/reference/orchestrator-cli.md still "
            "references the deprecated aggregate filename pattern "
            "`{identifier}-implement.{md,json}` outside an allow-listed "
            "historical context. Update each line to the per-slice "
            "filename or wrap it in an explicit historical framing."
            f" Offending lines:\n{_format_lines(offending)}"
        )


# ---------------------------------------------------------------------------
# task-1-2: docs/guides/concurrent-execution.md pinning
# ---------------------------------------------------------------------------


class TestConcurrentExecutionContextPrSection:
    """`docs/guides/concurrent-execution.md` must contain a "Context
    PR" heading or section so readers landing on the operator guide can
    find the new mechanism without having to grep through the prose.
    Acceptance criterion for task-1-2 (#2548)."""

    @pytest.fixture(scope="class")
    def text(self) -> str:
        return _read(GUIDES_CONCURRENT_EXECUTION)

    def test_has_context_pr_heading(self, text: str) -> None:
        # Any markdown heading level (`#` ... `####`) whose trimmed
        # body contains "Context PR" qualifies. We also accept a
        # heading like "Context PR is opened first" (the planner's
        # explicit suggestion in task-1-2).
        heading_pattern = re.compile(
            r"^\s{0,3}#{1,6}\s+.*?Context PR.*$",
            flags=re.MULTILINE | re.IGNORECASE,
        )
        match = heading_pattern.search(text)
        assert match is not None, (
            "task-1-2: docs/guides/concurrent-execution.md must contain a "
            "`Context PR` markdown heading (any level, e.g. "
            "`### Context PR is opened first`). The contract requires an "
            "explicit subsection so the new PR-stack root is "
            "navigable. See issue #2548."
        )

    def test_describes_slice_1_stacks_on_context(self, text: str) -> None:
        # The acceptance criteria call for "describe the new slice-1 base
        # resolution (slice-1 stacks on `egg/<id>/context`)". Require
        # the literal context-branch path (any of the common
        # placeholder forms) so a generic "slice-1 ... base" mention
        # elsewhere in the doc does not accidentally satisfy the test.
        has_branch_literal = (
            "egg/<id>/context" in text
            or "egg/{id}/context" in text
            or "egg/{identifier}/context" in text
        )
        assert has_branch_literal, (
            "task-1-2: docs/guides/concurrent-execution.md must describe "
            "the new slice-1 base resolution by naming the context "
            "branch literally (e.g. `egg/<id>/context`). Without this "
            "the doc still implies slice-1's base is `egg/<id>/work`, "
            "which contradicts the post-#2548 stack shape."
        )

    def test_slice_1_paragraph_ties_to_context_branch(self, text: str) -> None:
        """Adversarial probe: the literal `egg/<id>/context` token
        could land in an unrelated paragraph (e.g. a sidebar that
        discusses the context branch but does not connect it to
        slice-1's base resolution). Require that at least one
        blank-line-delimited paragraph contains both the branch
        literal AND a `slice-1` mention, so the two ideas live in the
        same paragraph rather than merely within textual proximity.
        """
        branch_tokens = (
            "egg/<id>/context",
            "egg/{id}/context",
            "egg/{identifier}/context",
        )
        if not any(token in text for token in branch_tokens):
            # Skip if the prerequisite assertion hasn't been satisfied
            # yet — `test_describes_slice_1_stacks_on_context` will
            # surface that failure on its own with a clearer message.
            pytest.skip("context-branch literal not present yet; covered by sibling test")
        # Markdown paragraphs are blank-line-delimited. Splitting on
        # one-or-more blank lines lets list items, fenced code blocks,
        # and prose paragraphs each count as their own paragraph.
        paragraphs = re.split(r"\n\s*\n", text)
        tied = any(
            any(token in para for token in branch_tokens)
            and re.search(r"slice-1", para, flags=re.IGNORECASE)
            for para in paragraphs
        )
        assert tied, (
            "task-1-2: the literal `egg/<id>/context` reference must "
            "live in the same blank-line-delimited paragraph as a "
            "`slice-1` mention so readers connect the branch to the "
            "slice-1 base resolution."
        )

    def test_cross_references_issue_2548(self, text: str) -> None:
        assert "#2548" in text, (
            "task-1-2: docs/guides/concurrent-execution.md must "
            "cross-reference issue #2548 so the rationale is one click "
            "away. The contract task description requires this cross-ref."
        )


# ---------------------------------------------------------------------------
# Directory-scoped grep with allow-list (acceptance criterion for task-1-3)
# ---------------------------------------------------------------------------


# An allow-list of (relative_path, line_substring) pairs that are known
# to legitimately reference the deprecated aggregate filename. Each
# entry must match the substring on the line. New aggregate-filename
# references that are not on this list will fail the directory-wide
# grep and force the author to either remove the reference or expand
# the allow-list (with rationale).
#
# Rationale per-entry:
#
# * `docs/guides/concurrent-execution.md` — these explain the contrast
#   with the slice-aware mode (the historic aggregate file is shown to
#   anchor the migration narrative) and describe the non-slice mode
#   where the aggregate is still the canonical artifact. Both are
#   operational documentation.
#
# * `docs/guides/sdlc-pipeline.md` lines around 350-351 — the file-tree
#   reference shows the brc-history layout for non-slice pipelines.
#   The aggregate file is still emitted in that case, so the reference
#   is current operational documentation, not a deprecated mention.
DEPRECATED_FILENAME_ALLOWLIST: list[tuple[str, str]] = [
    (
        "docs/guides/concurrent-execution.md",
        "[`implement`](./.egg-state/brc-history/42-implement.md)",
    ),
    (
        "docs/guides/concurrent-execution.md",
        "{identifier}-implement.md` file is **not** produced",
    ),
    (
        "docs/guides/concurrent-execution.md",
        "Non-slice implement runs continue to emit the aggregate",
    ),
    (
        "docs/guides/sdlc-pipeline.md",
        "{identifier}-implement.md    # BRC consensus messages from implement phase",
    ),
    (
        "docs/guides/sdlc-pipeline.md",
        "{identifier}-implement.json  # BRC consensus messages from implement phase",
    ),
]


# Compile a regex that matches the deprecated filename pattern. We
# match an optional preceding ``identifier``-style prefix so we catch
# `{identifier}-implement.md`, `42-implement.md`, `<id>-implement.md`,
# etc. but do NOT match `-implement-slice-<N>.md` (the new pattern).
DEPRECATED_FILENAME_REGEX = re.compile(r"[\w{}<>\-]*-implement\.(?:md|json)\b")


def _find_deprecated_filename_lines(text: str) -> list[tuple[int, str]]:
    """Return the (line_number, line) pairs in `text` that mention the
    deprecated aggregate filename pattern.

    Skips the per-slice variant (`-implement-slice-<N>.md`) by relying
    on the regex above, which only matches the bare aggregate suffix.
    """
    hits: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if DEPRECATED_FILENAME_REGEX.search(raw):
            hits.append((lineno, raw))
    return hits


def _format_lines(hits: list[tuple[int, str]]) -> str:
    return "\n".join(f"  L{lineno}: {line.rstrip()}" for lineno, line in hits)


class TestDocsDirectoryDeprecatedFilenameGrep:
    """Directory-scoped grep across `docs/`: any reference to the
    deprecated aggregate filename pattern that is not on the
    allow-list fails this test. Acceptance criterion for task-1-3
    (#2548)."""

    def _iter_doc_files(self) -> list[Path]:
        return sorted(p for p in DOCS_ROOT.rglob("*.md") if p.is_file())

    def test_directory_scoped_grep_clean(self) -> None:
        unallowed: list[str] = []
        seen_allowlist_entries: set[tuple[str, str]] = set()
        for doc_file in self._iter_doc_files():
            rel = doc_file.relative_to(PROJECT_ROOT).as_posix()
            for lineno, line in _find_deprecated_filename_lines(_read(doc_file)):
                allowed_substr = self._lookup_allowlist(rel, line)
                if allowed_substr is not None:
                    seen_allowlist_entries.add((rel, allowed_substr))
                    continue
                unallowed.append(f"{rel}:{lineno}: {line.rstrip()}")
        assert not unallowed, (
            "task-1-3: directory-scoped grep across `docs/` found "
            "references to the deprecated aggregate filename pattern "
            "(`{identifier}-implement.{md,json}`) outside the "
            "explicit allow-list. Update the offending docs to use the "
            "per-slice variant or extend the allow-list with rationale.\n"
            "Offending lines:\n" + "\n".join(f"  {hit}" for hit in unallowed)
        )

    def test_allowlist_has_no_stale_entries(self) -> None:
        """Adversarial probe: prevent the allow-list from rotting.

        If a future doc edit removes a legitimately-allowed reference
        but forgets to remove the corresponding allow-list entry, the
        list grows stale and a future regression could slip past. This
        test fails when an allow-list entry no longer matches any line
        in the named file.
        """
        stale: list[tuple[str, str]] = []
        for rel, substr in DEPRECATED_FILENAME_ALLOWLIST:
            doc_path = PROJECT_ROOT / rel
            if not doc_path.exists():
                stale.append((rel, substr))
                continue
            text = _read(doc_path)
            if substr not in text:
                stale.append((rel, substr))
        assert not stale, (
            "DEPRECATED_FILENAME_ALLOWLIST entries are stale (the "
            "referenced substring no longer appears in the named file). "
            "Remove the stale entries so the allow-list cannot mask "
            "future regressions.\nStale entries:\n"
            + "\n".join(f"  {rel}: {substr!r}" for rel, substr in stale)
        )

    @staticmethod
    def _lookup_allowlist(rel_path: str, line: str) -> str | None:
        """Return the allow-list substring that matches the line, or
        None if no entry covers it."""
        for entry_path, substr in DEPRECATED_FILENAME_ALLOWLIST:
            if entry_path == rel_path and substr in line:
                return substr
        return None


# ---------------------------------------------------------------------------
# Adversarial probe: regex itself behaves correctly
# ---------------------------------------------------------------------------


class TestDeprecatedFilenameRegex:
    """Adversarial probes for the regex that classifies deprecated
    aggregate-filename references. Without these, a regex tweak that
    silently broadens or narrows the match could let regressions slip
    past the docs grep."""

    @pytest.mark.parametrize(
        "line",
        [
            "see {identifier}-implement.md for context",
            "the file `42-implement.md` describes the implement phase",
            "<id>-implement.json holds the JSON companion",
            # Mixed surrounding punctuation
            "{identifier}-implement.md.",
            "(`{identifier}-implement.json`)",
        ],
    )
    def test_regex_matches_aggregate(self, line: str) -> None:
        assert DEPRECATED_FILENAME_REGEX.search(line) is not None, (
            f"regex must match deprecated aggregate filename in: {line!r}"
        )

    @pytest.mark.parametrize(
        "line",
        [
            # Per-slice variants must NOT match — they are the new
            # canonical filenames.
            "{identifier}-implement-slice-1.md",
            "42-implement-slice-12.json",
            "see `<id>-implement-slice-2.md`",
            "{identifier}-implement-unattributed.md",
            # Other phases must NOT match.
            "{identifier}-refine.md",
            "{identifier}-plan.json",
            # Loose substrings that contain the word but not the pattern.
            "the implement phase",
            "during implementation",
            "implementation.md is unrelated",
        ],
    )
    def test_regex_rejects_non_deprecated(self, line: str) -> None:
        assert DEPRECATED_FILENAME_REGEX.search(line) is None, (
            f"regex must NOT match (no deprecated filename) in: {line!r}"
        )


# ---------------------------------------------------------------------------
# Smoke tests: the four files described by slice-1 still exist
# ---------------------------------------------------------------------------


class TestSliceOneDocsExist:
    @pytest.mark.parametrize(
        "path",
        [
            ARCHITECTURE_ORCHESTRATOR,
            REFERENCE_ORCHESTRATOR_CLI,
            GUIDES_CONCURRENT_EXECUTION,
        ],
    )
    def test_doc_file_exists(self, path: Path) -> None:
        assert path.exists(), (
            f"slice-1 doc file is missing: {path}. The four files named "
            "in tasks 1-1 and 1-2 must exist; if a file was renamed, "
            "update this test and the contract together."
        )
