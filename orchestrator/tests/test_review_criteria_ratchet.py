"""Content ratchet for the slice-1 reviewer-criteria additions (#3523 S1, task-1-3).

Issue #3523 slice-1 ("Reviewer method-angle procedures & verification ladder")
is a prompt-only change to `shared/prompts/*-criteria.md`. It adds two things,
verbatim from the operator directive (the issue body, items 2 and 3) and mirrored
onto the Claude Code `/review` skill vocabulary:

1. Four named **method-angle search procedures** — the *how to look* layer — added
   to the code-reviewer criteria (task-1-1):
     * "Line-by-line scan"       — per changed line, what makes it wrong.
     * "Removed-behavior audit"  — per deleted/replaced line, name the invariant
                                    it enforced and find where it is re-established.
     * "Cross-file tracer"       — grep callers/callees for broken call sites.
     * "Quote-the-rule discipline" — flag a convention violation only when BOTH the
                                    written rule and the violating line can be quoted.

2. The three-state **verification ladder** (CONFIRMED / PLAUSIBLE / REFUTED) with its
   two **companion rules** — "blocking must reproduce" and "drop only the refuted;
   downgrade the unconfirmed" — appended to every specialist criteria file so every
   lens shares the same verify discipline (task-1-1 + task-1-2).

This module is the **ratchet**: if any required procedure name, ladder verdict, or
companion-rule phrase is later deleted from its criteria file, `make test` fails
before the prompt content can silently regress. It is a pure content check — grep /
substring assertions over the file bytes, no runtime orchestration (task-1-3).

File coverage (authoritative, from the documenter tasks task-1-1 / task-1-2):

* Method-angle procedures live in the code-reviewer criteria only:
  `code-review-criteria.md`, `code-review-holistic-criteria.md`.
* The verification ladder + companion rules live in EVERY specialist criteria file
  that carries the ladder: the two above plus `security-review-criteria.md`,
  `concurrency-review-criteria.md`, `agent-design-criteria.md`,
  `contract-review-criteria.md`.

Match discipline:

* Ladder verdicts are the fixed uppercase vocabulary tokens — matched word-bounded
  and case-sensitive (`\bCONFIRMED\b`) so a lowercase reword or a partial token does
  not satisfy the ratchet.
* Procedure names and companion-rule phrases are matched as case-insensitive
  substrings: the ratchet locks the distinctive phrase, tolerant of surrounding
  markup (bold markers, headings) and title-vs-sentence casing, but not tolerant of
  the phrase's removal.
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


# ---------------------------------------------------------------------------
# Required content — the S1 vocabulary, fixed by the issue body + documenter tasks
# ---------------------------------------------------------------------------

# The four method-angle procedures (issue item 3). Distinctive multi-word phrases;
# matched case-insensitively so surrounding markup / casing cannot evade the lock.
_METHOD_ANGLE_PROCEDURES: tuple[str, ...] = (
    "Line-by-line scan",
    "Removed-behavior audit",
    "Cross-file tracer",
    "Quote-the-rule discipline",
)

# The three verification-ladder verdicts (issue item 2). Fixed uppercase tokens.
_LADDER_VERDICTS: tuple[str, ...] = (
    "CONFIRMED",
    "PLAUSIBLE",
    "REFUTED",
)

# The two companion rules (issue item 2). Each rule is required as a whole: every
# substring in its tuple must be present (case-insensitive). Rule 2 is split into
# its two halves so a variant that keeps only one clause still fails the ratchet.
_COMPANION_RULES: tuple[tuple[str, ...], ...] = (
    ("blocking must reproduce",),
    ("drop only the refuted", "downgrade the unconfirmed"),
)

# Files carrying the method-angle procedures (code-reviewer criteria; task-1-1).
_PROCEDURE_FILES: tuple[str, ...] = (
    "code-review-criteria.md",
    "code-review-holistic-criteria.md",
)

# Files carrying the verification ladder + companion rules — every specialist
# criteria file (task-1-1 + task-1-2).
_LADDER_FILES: tuple[str, ...] = (
    "code-review-criteria.md",
    "code-review-holistic-criteria.md",
    "security-review-criteria.md",
    "concurrency-review-criteria.md",
    "agent-design-criteria.md",
    "contract-review-criteria.md",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_criteria(basename: str) -> str:
    """Read a criteria file's bytes as text. A missing file is itself a ratchet
    failure (the S1 content must exist), surfaced with the resolved path so the
    breakage is unambiguous."""
    path = _SHARED_PROMPTS_DIR / basename
    assert path.is_file(), (
        f"required criteria file missing: {path.relative_to(_REPO_ROOT)}; "
        f"slice-1 (#3523) must add the verification ladder to it."
    )
    return path.read_text(encoding="utf-8")


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_ws(text: str) -> str:
    """Collapse every run of whitespace (spaces, tabs, newlines) to a single
    space. Markdown prose wraps multi-word phrases across line breaks, so the
    phrase matcher must compare on normalized whitespace or a wrapped phrase
    (e.g. "downgrade the\\nunconfirmed") would evade the ratchet even though the
    content is present."""
    return _WHITESPACE_RE.sub(" ", text)


def _contains_phrase(text: str, phrase: str) -> bool:
    """Case-insensitive, whitespace-normalized substring presence — used for the
    multi-word procedure names and companion-rule phrases, tolerant of surrounding
    markup, casing, and line-wrapping."""
    return _normalize_ws(phrase).casefold() in _normalize_ws(text).casefold()


def _contains_verdict(text: str, verdict: str) -> bool:
    """Word-bounded, case-sensitive presence — used for the fixed uppercase ladder
    verdict tokens so a lowercase reword does not satisfy the ratchet."""
    return re.search(rf"\b{re.escape(verdict)}\b", text) is not None


# ---------------------------------------------------------------------------
# Ratchet tests against the live tree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basename", _PROCEDURE_FILES, ids=lambda b: b)
def test_method_angle_procedures_present(basename: str) -> None:
    """Each code-reviewer criteria file must name all four method-angle search
    procedures. Removing any one fails the ratchet (task-1-3 acceptance)."""
    text = _read_criteria(basename)
    missing = [p for p in _METHOD_ANGLE_PROCEDURES if not _contains_phrase(text, p)]
    assert not missing, (
        f"{basename} is missing method-angle procedure name(s): {missing}. "
        f"Slice-1 (#3523 item 3) requires all of {list(_METHOD_ANGLE_PROCEDURES)} "
        f"to be present verbatim; do not reword or drop them."
    )


@pytest.mark.parametrize("basename", _LADDER_FILES, ids=lambda b: b)
def test_ladder_verdicts_present(basename: str) -> None:
    """Every specialist criteria file that carries the ladder must name all three
    verdict states. Removing any one fails the ratchet (task-1-3 acceptance)."""
    text = _read_criteria(basename)
    missing = [v for v in _LADDER_VERDICTS if not _contains_verdict(text, v)]
    assert not missing, (
        f"{basename} is missing verification-ladder verdict(s): {missing}. "
        f"Slice-1 (#3523 item 2) requires the uppercase tokens "
        f"{list(_LADDER_VERDICTS)} in every ladder-bearing criteria file."
    )


@pytest.mark.parametrize("basename", _LADDER_FILES, ids=lambda b: b)
def test_companion_rules_present(basename: str) -> None:
    """Every ladder-bearing criteria file must carry both companion-rule phrases
    ("blocking must reproduce"; "drop only the refuted; downgrade the
    unconfirmed"). Removing either fails the ratchet (task-1-3 acceptance)."""
    text = _read_criteria(basename)
    missing_rules: list[tuple[str, ...]] = [
        rule for rule in _COMPANION_RULES if not all(_contains_phrase(text, part) for part in rule)
    ]
    assert not missing_rules, (
        f"{basename} is missing companion-rule phrase(s): {missing_rules}. "
        f"Slice-1 (#3523 item 2) requires both companion rules — "
        f"'blocking must reproduce' and 'drop only the refuted; downgrade the "
        f"unconfirmed' — in every ladder-bearing criteria file."
    )


# ---------------------------------------------------------------------------
# Test-for-the-test — prove the matchers are sharp (independent of live content).
# A regression that weakened a matcher (e.g. dropped the word boundary, made the
# verdict match case-insensitive) would be caught here even if the live files
# still happened to contain the tokens.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", _METHOD_ANGLE_PROCEDURES)
def test_phrase_matcher_fires_and_is_case_insensitive(phrase: str) -> None:
    """The phrase matcher must find each procedure name regardless of casing and
    surrounding markup, and must NOT find it once removed."""
    assert _contains_phrase(f"- **{phrase.upper()}**: how to look", phrase)
    assert _contains_phrase(f"prose mentioning {phrase.lower()} inline", phrase)
    assert not _contains_phrase("a criteria file with none of the procedures", phrase)


def test_phrase_matcher_tolerates_line_wrapping() -> None:
    """A multi-word phrase wrapped across a markdown line break must still match:
    the documenter's holistic criteria wraps "downgrade the\\nunconfirmed", and
    the content is present even though a raw substring search for the spaced form
    would miss it."""
    wrapped = "carry PLAUSIBLE as advisory: drop only the refuted; downgrade the\nunconfirmed findings rather than dropping them."
    assert _contains_phrase(wrapped, "drop only the refuted")
    assert _contains_phrase(wrapped, "downgrade the unconfirmed")


@pytest.mark.parametrize("verdict", _LADDER_VERDICTS)
def test_verdict_matcher_is_word_bounded_and_case_sensitive(verdict: str) -> None:
    """The verdict matcher must fire on the standalone uppercase token, and must
    NOT fire on a lowercase reword or a token embedded in a larger word."""
    assert _contains_verdict(f"a **{verdict}** blocking finding NACKs", verdict)
    assert not _contains_verdict(f"the {verdict.lower()} state", verdict)
    assert not _contains_verdict(f"UN{verdict}ED", verdict)


def test_companion_rule_requires_all_parts() -> None:
    """Rule 2 must require BOTH halves — a text with only one clause fails."""
    rule2 = _COMPANION_RULES[1]
    both = "Drop only the refuted; downgrade the unconfirmed rather than dropping it."
    only_first = "Drop only the refuted claims."
    assert all(_contains_phrase(both, part) for part in rule2)
    assert not all(_contains_phrase(only_first, part) for part in rule2)


def test_ladder_files_superset_of_procedure_files() -> None:
    """Structural invariant: every file carrying the method-angle procedures also
    carries the ladder (the code-reviewer criteria get both). Guards against the
    two file lists drifting out of the documented relationship."""
    assert set(_PROCEDURE_FILES).issubset(set(_LADDER_FILES))
