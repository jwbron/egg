"""Blocking-wait instruction ratchet for agent-facing prompt sources (#3157).

The event-pump contract (#2908) is: **agents never wait on the message
bus** — the consensus wrapper (`orchestrator/consensus_wrapper.py`) owns
every blocking read (`egg-orch message wait` / `wait-loop`) and invokes
the agent one-shot per actionable BRC event. The per-event prompt
(`orchestrator/routes/event_prompt.py`) states the contract to the agent
directly; `docs/reference/agent-wait-patterns.md` §0 is the reference.

#3157 retired the pre-pump agent-facing canon that still taught the
"STAY ALIVE" blocking-wait idiom. This module is the ratchet: any future
reintroduction of a blocking-wait instruction into the scanned
agent-facing sources fails CI before the contract can drift back —
recreating the #1897 pathologies and double-waiting against the wrapper.

Scope (the sources that reach agents):

* `shared/prompts/*.md` — every agent-facing markdown prompt template.
* `sandbox/agent-config/rules/*.md` — combined into every sandbox
  agent's `~/.claude/CLAUDE.md` at container startup (see the README in
  that directory).
* prompt-constructing strings in `orchestrator/routes/event_prompt.py`.

Patterns matched:

* `egg-orch message wait` / `egg-orch message wait-loop` — the blocking
  CLI surface is wrapper-internal; agent-facing text may name it only to
  forbid it (those occurrences are allowlisted with justification).
* `STAY ALIVE` / `stay-alive` / `stay_alive` — the retired idiom name.
  Agent-facing text must not present it as a live step.

Patterns NOT matched (intentionally):

* `egg-orch message poll` — a bounded, non-blocking read. The overseer's
  monitoring loop uses it legitimately.
* `wait_for_event` as a generic concurrency term (e.g. in
  `concurrency-review-criteria.md` describing deadlocks in *reviewed*
  code) — only the `egg-orch message wait` CLI surface is the contract
  boundary.
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
_AGENT_RULES_DIR = _REPO_ROOT / "sandbox" / "agent-config" / "rules"
_EVENT_PROMPT_PATH = _REPO_ROOT / "orchestrator" / "routes" / "event_prompt.py"


# ---------------------------------------------------------------------------
# Match patterns
# ---------------------------------------------------------------------------

# The blocking CLI surface. `\s+` between tokens so a prose line break
# cannot evade the ratchet; the optional `-loop` suffix covers both
# verbs with one pattern.
_BLOCKING_WAIT_CLI = re.compile(r"\begg-orch\s+message\s+wait(?:-loop)?\b", re.IGNORECASE)

# The retired idiom name. Hyphen/underscore/space variants; word-bounded
# so prose like "stays alive" or "Stay Aligned" passes through.
_STAY_ALIVE = re.compile(r"\bstay[-_ ]alive\b", re.IGNORECASE)

_SCAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("blocking-wait CLI (`egg-orch message wait[-loop]`)", _BLOCKING_WAIT_CLI),
    ("retired STAY ALIVE idiom", _STAY_ALIVE),
)


# ---------------------------------------------------------------------------
# Justified allowlist
# ---------------------------------------------------------------------------
#
# Same schema and review contract as `test_prompt_sync_ratchet.py`:
# (source_relpath, matched_substring, line_number_1_indexed), with a
# JUSTIFICATION comment immediately above each row. The source key is
# the POSIX repo-relative path (not the basename) so a future
# same-named file added to the other scanned directory cannot make a
# line-pin mis-apply across files. The pinned line number makes a
# drifted entry fail, forcing re-verification of the justification. The
# only legitimate occurrences are NEGATIVE instructions — text that
# names the forbidden command in order to forbid it.
_ALLOWLIST: tuple[tuple[str, str, int], ...] = (
    # JUSTIFICATION: mission.md tells the agent it does NOT need to call
    # `egg-orch message wait` because the wrapper owns sequencing — the
    # token appears only inside the negative instruction that codifies
    # the event-pump contract (#2908 slice-3 mission rewrite).
    ("sandbox/agent-config/rules/mission.md", "egg-orch message wait", 155),
    # JUSTIFICATION: the rules/orchestrator.md CLI-reference callout
    # forbids agent-tier blocking waits (#3157); it names
    # `egg-orch message wait` / `wait-loop` only to mark them
    # wrapper-internal and direct the agent to exit instead. Two
    # matches on the same line (the wrapper attribution and the
    # "do NOT run" clause) — both covered by this entry.
    ("sandbox/agent-config/rules/orchestrator.md", "egg-orch message wait", 64),
    # JUSTIFICATION: the per-event prompt contract string is the
    # authoritative agent-facing wording of the invariant — "Do NOT
    # block on ``egg-orch message wait-loop`` yourself: the wrapper
    # owns the wait and the heartbeat (#2908 slice-2)". The token is
    # present precisely to forbid the call.
    ("orchestrator/routes/event_prompt.py", "egg-orch message wait-loop", 829),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _markdown_files(directory: Path) -> list[Path]:
    """Enumerate every `.md` file in an agent-facing prompt directory.
    Future additions are auto-covered — no test edit required."""
    return sorted(directory.glob("*.md"))


def _scan_text(text: str) -> list[tuple[str, int, str]]:
    """Return findings as `(pattern_label, line_number, matched_text)`
    tuples. Line numbers are 1-indexed."""
    findings: list[tuple[str, int, str]] = []
    for label, pattern in _SCAN_PATTERNS:
        for match in pattern.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            findings.append((label, line_number, match.group(0)))
    return findings


def _filter_allowlist(
    source_relpath: str, findings: list[tuple[str, int, str]]
) -> list[tuple[str, int, str]]:
    """Drop allowlisted findings; surface any remainder. `source_relpath`
    is the POSIX repo-relative path so a line-pin cannot mis-apply across
    same-named files in different scanned directories."""
    survivors: list[tuple[str, int, str]] = []
    for label, line_number, matched in findings:
        allowlisted = any(
            allow_src == source_relpath and allow_text in matched and allow_line == line_number
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
    _markdown_files(_SHARED_PROMPTS_DIR) + _markdown_files(_AGENT_RULES_DIR),
    ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_agent_facing_markdown_has_no_wait_instructions(prompt_path: Path) -> None:
    """No agent-facing markdown source may instruct a blocking bus wait
    or present the STAY ALIVE idiom as live. The wrapper owns all
    waiting (#2908); agents handle one event and exit (#3157)."""
    text = prompt_path.read_text(encoding="utf-8")
    findings = _filter_allowlist(prompt_path.relative_to(_REPO_ROOT).as_posix(), _scan_text(text))
    assert not findings, (
        f"blocking-wait instruction in {prompt_path.name}: {findings}; "
        f"agents never wait on the bus — the consensus wrapper owns "
        f"`egg-orch message wait[-loop]` and invokes agents one-shot "
        f"per event (docs/reference/agent-wait-patterns.md §0). If the "
        f"occurrence is a negative instruction (naming the command to "
        f"forbid it), add a justified _ALLOWLIST entry."
    )


def test_event_prompt_has_no_wait_instructions() -> None:
    """The per-event prompt composer must not instruct blocking waits.
    The single allowlisted occurrence is the contract sentence that
    forbids them."""
    text = _EVENT_PROMPT_PATH.read_text(encoding="utf-8")
    findings = _filter_allowlist(
        _EVENT_PROMPT_PATH.relative_to(_REPO_ROOT).as_posix(), _scan_text(text)
    )
    assert not findings, (
        f"blocking-wait instruction in "
        f"{_EVENT_PROMPT_PATH.relative_to(_REPO_ROOT)}: {findings}; "
        f"the composer's only sanctioned mention is the negative "
        f"contract sentence ('Do NOT block on ... yourself'). New "
        f"occurrences either belong in wrapper-tier code or need a "
        f"justified _ALLOWLIST entry."
    )


# ---------------------------------------------------------------------------
# Inverse-ratchet tests — prove the patterns are sharp.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "imperative, family",
    [
        ("Run `egg-orch message wait --for CONSENSUS_PROPOSE`.", "blocking-wait CLI"),
        ("egg-orch message wait-loop --for CONSENSUS_CONFIRMED", "blocking-wait CLI"),
        # Whitespace / case variants — a tab or line break between
        # tokens must not evade the ratchet.
        ("egg-orch\tmessage\twait-loop --for STATUS", "blocking-wait CLI"),
        ("EGG-ORCH MESSAGE WAIT --for STATUS", "blocking-wait CLI"),
        ("enter your STAY ALIVE step and block", "retired idiom"),
        ("the stay-alive loop keeps the agent resident", "retired idiom"),
        ("stay_alive until consensus closes", "retired idiom"),
    ],
)
def test_ratchet_fires_on_reintroduced_wait_instructions(imperative: str, family: str) -> None:
    """Mutation harness: simulate reintroducing a wait instruction into
    a prompt-source-shaped string and assert the ratchet fires."""
    findings = _scan_text(imperative)
    assert findings, (
        f"ratchet failed to match {family} sample {imperative!r}; "
        f"the pattern set must catch this imperative or the ratchet "
        f"provides no regression coverage."
    )


@pytest.mark.parametrize(
    "rendered, kind",
    [
        # Bounded, non-blocking reads stay legal.
        ("egg-orch message poll --role overseer --wait 30", "bounded poll"),
        # Prose near-misses must pass through.
        ("the pod stays alive until SIGTERM", "prose `stays alive`"),
        ("What Must Stay Aligned", "prose `Stay Aligned`"),
        # Generic concurrency vocabulary in review criteria is not the
        # CLI surface.
        ("reviewer B's wait_for_event is blocked on a message", "generic wait term"),
        # Heartbeat emission is not a wait.
        ("egg-orch message heartbeat --state WORKING", "heartbeat"),
    ],
)
def test_ratchet_passes_through_legal_text(rendered: str, kind: str) -> None:
    """The ratchet must NOT fire on bounded polls, heartbeats, generic
    concurrency vocabulary, or prose near-misses — otherwise the tree
    cannot stay green and every reference gets allowlisted until the
    ratchet is meaningless."""
    findings = _scan_text(rendered)
    assert not findings, f"ratchet false-positive on {kind} sample {rendered!r}: {findings}"


# ---------------------------------------------------------------------------
# Allowlist hygiene
# ---------------------------------------------------------------------------


def test_allowlist_entries_carry_justification_shape() -> None:
    """Each `_ALLOWLIST` entry must be a (source_relpath,
    matched_substring, line_number) triple so the justification comment
    immediately above it is locatable on review, and the pinned line
    forces re-verification when the source moves."""
    for entry in _ALLOWLIST:
        assert len(entry) == 3, (
            f"_ALLOWLIST entry {entry!r} must be a "
            f"(source_relpath, matched_substring, line_number) triple."
        )
        source, matched, line_number = entry
        assert isinstance(source, str) and source
        assert isinstance(matched, str) and matched
        assert isinstance(line_number, int) and line_number > 0
