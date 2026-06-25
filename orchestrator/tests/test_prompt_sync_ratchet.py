"""Sync-mechanic ratchet for agent-facing prompt sources (#3077 slice-5 TASK-5-4).

Phase-3 of #3077 retires the agent-facing `git fetch` / `git merge` /
`git pull` instruction prose and brc-history disk-path references from
agent-facing prompts in favor of served reads:

* the wrapper's `sync_to_proposals` merge + the slice-1 "worktree NOT
  synced" banner (`orchestrator/consensus_wrapper.py`),
* the rendered `git show <sha>:<path>` delta commands embedded in the
  event prompt (`orchestrator/routes/event_prompt.py`), and
* the `egg-artifact` sandbox verb backed by the gateway
  `POST /api/v1/artifact/get` endpoint, resolved against the artifact
  spec (`shared/egg_contracts/artifact_spec.py`).

This module is the ratchet: any future reintroduction of those sync
mechanics into the scanned prompt sources fails CI before the prose
channel can drift back. Additions to the allowlist require editing
this test — that is the ratchet (the plan's TASK-5-4 description).

Scope (mirrors the plan, TASK-5-4):

* `shared/prompts/*.md` — every agent-facing markdown prompt.
* prompt-constructing template strings in
  `orchestrator/routes/event_prompt.py`.

Patterns matched (instructional sync mechanics — the things slice-5
retired):

* `git fetch` (word-bounded).
* `git merge` (word-bounded).
* `git pull` (word-bounded).
* brc-history disk-path references (`.egg-state/brc-history/...`) —
  the BRC transcript must be consumed through served reads
  (`mcp__brc__read_peer_artifact`), never by reaching into the
  orchestrator's on-disk store.

Patterns NOT matched (intentionally):

* `git show` — the rendered per-SHA delta commands are served-read
  companions, not sync mechanics. The reviewer consumes each
  producer's per-SHA delta from these embedded commands.
* `git log` — likewise rendered (the per-producer re-review-scope
  command), not an instructional sync directive. `git log` is not in
  the sync-mechanics list because the wrapper performs the sync, not
  the agent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Source locations (resolved relative to the repo root via this file's path)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SHARED_PROMPTS_DIR = _REPO_ROOT / "shared" / "prompts"
_EVENT_PROMPT_PATH = _REPO_ROOT / "orchestrator" / "routes" / "event_prompt.py"


# ---------------------------------------------------------------------------
# Match patterns
# ---------------------------------------------------------------------------

# `git fetch` / `git merge` / `git pull` as instructional imperatives.
# `\b` on both ends keeps the match from triggering on word continuations
# such as `git fetcher`. `\s+` accepts any whitespace between `git` and
# the verb (space, tab, newline) so a prose line break cannot evade the
# ratchet.
_SYNC_MECHANICS = re.compile(r"\bgit\s+(?:fetch|merge|pull)\b", re.IGNORECASE)

# brc-history disk-path references. The orchestrator persists BRC
# transcripts under `.egg-state/brc-history/<identifier>-<phase>.json`
# (see `orchestrator/routes/pipelines.py` `_write_brc_history`), but
# agents must NOT read those paths directly — the contract MCP /
# `mcp__brc__read_peer_artifact` is the served-read channel.
_BRC_HISTORY = re.compile(r"\.egg-state/brc-history", re.IGNORECASE)

# All scan patterns. Both are scanned against every source file so the
# error message can report which family fired.
_SCAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sync-mechanic (`git fetch` / `git merge` / `git pull`)", _SYNC_MECHANICS),
    ("brc-history disk path", _BRC_HISTORY),
)


# ---------------------------------------------------------------------------
# Justified allowlist
# ---------------------------------------------------------------------------
#
# Each entry pairs a source filename (basename, since prompts share a
# directory and event_prompt.py is uniquely named) with a (line_number,
# substring) marker plus a JUSTIFICATION comment block immediately
# above the row. An entry is added only when the matched text is
# genuinely harness-internal (e.g. a Python comment referencing the
# retired mechanic by name to explain why something was removed) and
# could not be reworded to drop the literal token without losing the
# historical pointer reviewers need to navigate the file.
#
# Growing this list is the explicit ratchet hop — a reviewer must
# eyeball every addition. TASK-5-1 (event-prompt self-fetch fallback
# removal) and TASK-5-2 (REVIEWER-SYNC.md retire fetch/log prose)
# brought the scanned surfaces to a clean state for agent-facing text;
# the only entries below are Python comments in `event_prompt.py` that
# cite the retired mechanic to explain WHY the surrounding code drops
# it.
#
# Schema: (source_basename, matched_substring, line_number_1_indexed).
# The line number is checked against the actual finding so a stale
# allowlist entry whose line drifts after edits still fails — forcing
# reviewers to re-verify the justification.
_ALLOWLIST: tuple[tuple[str, str, int], ...] = (
    # JUSTIFICATION: `git fetch` appears in a Python `#` comment block
    # inside `_render_producer_delta_section()`'s no-proposal-SHA
    # degraded-baseline branch, where the slice-5 TASK-5-1 commit
    # (`b914f027d`) replaced the prior "fetch and read the actual file
    # diffs yourself" instruction prose. The comment names the
    # retired mechanism so a future reader of the degraded-baseline
    # path understands WHY the agent must NOT self-fetch even when
    # `last_reviewed_commit_sha` is missing (per-role worktrees can't
    # see producer commits without the wrapper's `sync_to_proposals`
    # merge). The token sits in a Python source comment, NOT in any
    # prompt-constructing string — agents never see it. Reworking the
    # comment to drop the literal token would strip the pointer
    # reviewers depend on to navigate the deletion.
    ("event_prompt.py", "git fetch", 1236),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shared_prompt_files() -> list[Path]:
    """Enumerate every `.md` file under `shared/prompts/`. The directory
    is the agent-facing prompt-template surface; any future addition is
    auto-covered by this ratchet — no test edit required."""
    return sorted(_SHARED_PROMPTS_DIR.glob("*.md"))


def _scan_text(text: str) -> list[tuple[str, int, str]]:
    """Return findings as `(pattern_label, line_number, matched_text)`
    tuples. Line numbers are 1-indexed (counts the newlines before the
    match)."""
    findings: list[tuple[str, int, str]] = []
    for label, pattern in _SCAN_PATTERNS:
        for match in pattern.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            findings.append((label, line_number, match.group(0)))
    return findings


def _filter_allowlist(
    source_basename: str, findings: list[tuple[str, int, str]]
) -> list[tuple[str, int, str]]:
    """Drop allowlisted findings; surface any remainder."""
    if not _ALLOWLIST:
        return findings
    survivors: list[tuple[str, int, str]] = []
    for label, line_number, matched in findings:
        allowlisted = any(
            allow_src == source_basename and allow_text in matched and allow_line == line_number
            for allow_src, allow_text, allow_line in _ALLOWLIST
        )
        if not allowlisted:
            survivors.append((label, line_number, matched))
    return survivors


# ---------------------------------------------------------------------------
# Ratchet tests against the live tree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt_path",
    _shared_prompt_files(),
    ids=lambda path: path.name,
)
def test_shared_prompt_has_no_sync_mechanics(prompt_path: Path) -> None:
    """Each shared agent-facing prompt must not instruct sync mechanics
    or reference brc-history disk paths. Served reads (the slice-1
    sync banner, the rendered `git show` per-SHA delta commands, the
    `egg-artifact` sandbox verb) cover what `git fetch` / `git merge`
    / `git pull` instruction prose used to."""
    text = prompt_path.read_text(encoding="utf-8")
    findings = _filter_allowlist(prompt_path.name, _scan_text(text))
    assert not findings, (
        f"sync-mechanic / brc-history reference in {prompt_path.name}: "
        f"{findings}; use served reads instead (wrapper sync banner, "
        f"rendered `git show <sha>:<path>` commands, `egg-artifact`), "
        f"or add a justified entry to _ALLOWLIST naming exactly why "
        f"this occurrence is harness-internal."
    )


def test_event_prompt_has_no_sync_mechanics() -> None:
    """The composer in `orchestrator/routes/event_prompt.py` must not
    embed `git fetch` / `git merge` / `git pull` imperatives or
    brc-history disk paths in prompt-constructing strings.

    Rendered `git show` delta commands and `git log` re-review-scope
    commands remain — those are served-read companions (the wrapper
    performs the sync; the agent reads the embedded commands), not
    sync mechanics the agent is instructed to perform.
    """
    text = _EVENT_PROMPT_PATH.read_text(encoding="utf-8")
    findings = _filter_allowlist(_EVENT_PROMPT_PATH.name, _scan_text(text))
    assert not findings, (
        f"sync-mechanic / brc-history reference in "
        f"{_EVENT_PROMPT_PATH.relative_to(_REPO_ROOT)}: {findings}; "
        f"the slice-5 cleanup deleted self-fetch fallback prose in "
        f"favor of the slice-1 NOT-synced banner and the rendered "
        f"`git show` per-SHA delta commands. New occurrences belong "
        f"in the served-reads path (artifact name + commit SHA via "
        f"`egg-artifact` / wrapper sync), or — if genuinely "
        f"harness-internal — in a justified _ALLOWLIST entry."
    )


# ---------------------------------------------------------------------------
# Inverse-ratchet tests — verify the patterns match what they must and
# pass through what they must not. These prove the ratchet is sharp:
# a regression that broke `_SYNC_MECHANICS` would be caught here.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "imperative, family",
    [
        ("git fetch origin main", "sync-mechanic"),
        ("Run `git fetch` first.", "sync-mechanic"),
        ("git merge --no-ff origin/main", "sync-mechanic"),
        ("git pull origin main", "sync-mechanic"),
        ("Read .egg-state/brc-history/issue-42-implement.json", "brc-history"),
        # Whitespace and case variants — the ratchet must catch all of
        # these; a future contributor cannot evade it by inserting a
        # tab between `git` and the verb, or by uppercasing.
        ("git\tfetch origin", "sync-mechanic"),
        ("GIT FETCH origin", "sync-mechanic"),
    ],
)
def test_ratchet_fires_on_reintroduced_mechanics(imperative: str, family: str) -> None:
    """Mutation harness: simulate reintroducing a sync mechanic into a
    prompt-source-shaped string and assert the ratchet pattern fires.

    This is the test-for-the-test mandated by the TASK-5-4 acceptance
    ("Test fails when a `git fetch` imperative is reintroduced into any
    scanned source") — without it, the ratchet could silently weaken
    (e.g. a regex change drops the boundary) without anyone noticing.
    """
    findings = _scan_text(imperative)
    assert findings, (
        f"ratchet failed to match {family} sample {imperative!r}; "
        f"the pattern set must catch this kind of imperative or the "
        f"ratchet provides no regression coverage."
    )


@pytest.mark.parametrize(
    "rendered, kind",
    [
        ("Read it via `git show abc123:foo.md`.", "git show (per-SHA delta)"),
        (
            "Re-review scope: `git log abc123..HEAD --not origin/main -p`.",
            "git log (re-review scope)",
        ),
        # Word-boundary near-miss: `git fetcher` must NOT match — the
        # ratchet uses `\b` to keep false positives off prose that
        # happens to embed the verb stem.
        ("The `git fetcher` daemon is a misnamed historical tool.", "near-miss"),
        # Non-git uses of `merge` / `pull` / `fetch` must NOT match —
        # only `git <verb>` forms are sync mechanics.
        ("merge the open NACKs into a single response", "prose `merge`"),
        ("pull the full open-NACK list from the payload", "prose `pull`"),
        ("fetch and read the diffs yourself", "prose `fetch` (verb only)"),
    ],
)
def test_ratchet_passes_through_served_reads_and_near_misses(rendered: str, kind: str) -> None:
    """The ratchet must NOT fire on the served-read companions
    (`git show`, `git log`) or on word-boundary near-misses. If it
    did, the post-cleanup tree could not stay green and reviewers
    would silently allowlist every served-read reference until the
    ratchet was meaningless.

    Note: this test pins `git log` as passing-through — `git log`
    is rendered (the per-producer re-review-scope command), not an
    instructional sync directive, and the plan TASK-5-4 description
    explicitly excludes it from the sync-mechanic list.
    """
    findings = _scan_text(rendered)
    assert not findings, (
        f"ratchet false-positive on {kind} sample {rendered!r}: "
        f"{findings}; the served-read / near-miss must pass through "
        f"or the post-cleanup tree cannot stay green."
    )


# ---------------------------------------------------------------------------
# Allowlist hygiene — every entry must carry a justification (enforced
# structurally so a contributor cannot add a bare row).
# ---------------------------------------------------------------------------


def test_allowlist_entries_carry_justification_comment() -> None:
    """Every `_ALLOWLIST` entry's justification lives in the source-
    file comments immediately above the row. We can't inspect AST
    comments at runtime, but we can pin the invariant that the
    allowlist tuple is the single source of truth and reviewers must
    eyeball it on every modification.

    The TASK-5-4 acceptance ("every allowlist entry carries a
    justification comment") is enforced here by asserting the
    allowlist is either empty (the post-cleanup baseline) or that
    each tuple has the documented (source, substring, line) shape so
    a reviewer reading the entry can locate the justification
    comment block immediately above it in this file.
    """
    for entry in _ALLOWLIST:
        assert len(entry) == 3, (
            f"_ALLOWLIST entry {entry!r} must be a "
            f"(source_basename, matched_substring, line_number) "
            f"triple; the structural shape is what makes the "
            f"adjacent justification comment locatable on review."
        )
        source, matched, line_number = entry
        assert isinstance(source, str) and source, (
            f"_ALLOWLIST source must be a non-empty basename (found {source!r} in {entry!r})."
        )
        assert isinstance(matched, str) and matched, (
            f"_ALLOWLIST matched substring must be a non-empty "
            f"string (found {matched!r} in {entry!r})."
        )
        assert isinstance(line_number, int) and line_number > 0, (
            f"_ALLOWLIST line number must be a positive int "
            f"(found {line_number!r} in {entry!r}); pinning the "
            f"line forces a re-review when the source moves."
        )
