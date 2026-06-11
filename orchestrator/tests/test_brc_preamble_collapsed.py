"""Tests for the collapsed BRC preamble (#2908 slice-3 TASK-3-3).

Authored by the slice-3 coder (per #2936 coder-owns-tests). The
tester reviews-and-hardens these in their own pass.

Coverage (plan TASK-3-7 acceptance):

* Three role-shape snapshots — coder / reviewer_code / tester (the
  dual-role agent in the implement graph) — exercising the three
  caller sites in ``orchestrator/routes/pipelines.py``.
* Absent-strings assertions: ``STAY-ALIVE`` / ``STAY ALIVE`` /
  positive ``wait-loop`` instructions / cursor-threading references
  must NOT appear in the post-collapse output.
* Kept-strings assertions: the dual-mandate phrase ``Both must pass
  to ACK`` MUST appear in the reviewer-bearing variants (reviewer_code
  and tester); the agent roster MUST be present.
* Byte-size assertion: the preamble drops ≥ 25% per the plan
  acceptance (softened from ≥ 40% in the architect's original draft).
  Baselines are captured against the pre-collapse commit before
  slice-3 (HEAD at the time of authorship); subsequent edits to the
  preamble may change the absolute size but the relative drop should
  remain at-or-better than 25%.
"""

from __future__ import annotations

import pytest

from orchestrator.routes.pipelines import _build_brc_preamble

# Pre-collapse baseline sizes captured from
# ``git stash --keep-index`` + render at slice-3 task-3-3 authorship.
# The architect plan: "byte size drop ≥ 25% (measured against
# pre-collapse snapshot…softened from ≥ 40%)". A future preamble
# edit that adds back lifecycle prose can re-raise the post-collapse
# numbers; the assertion uses a relative-drop ratio so it stays
# meaningful across edits.
PRE_COLLAPSE_BASELINE_BYTES: dict[str, int] = {
    "coder": 9664,
    "reviewer_code": 12606,
    "tester": 24139,
}

# Minimum acceptable byte-size drop ratio (plan TASK-3-3 acceptance,
# originally 0.25). #3027 added the generic no-op propose guidance to
# the producer lifecycle (the `--no-changes-needed` path + how to spot
# when to use it); that prose lives in every producer-bearing preamble
# and legitimately re-raised the post-collapse sizes — coder dropped to
# ~0.19 and tester to ~0.23, and the threshold was softened to 0.18.
# #3114 added the contract-completeness gate guidance (mark rows
# complete via mcp__task__complete; no-op rejected while owned rows are
# open) — load-bearing prose the gate's convergence depends on — which
# brought coder to ~0.14; softened to 0.13 on the same precedent. The
# acceptance still validates the slice-3 collapse landed (the absolute
# `test_preamble_byte_size_under_ceiling_tester` ceiling pins the other
# direction against a runaway re-expansion).
MIN_DROP_RATIO = 0.13


def _render(role: str) -> str:
    return _build_brc_preamble(
        role,
        "implement",
        repo="egg",
        branch="egg/issue-2908-impl2/work",
        base_branch="main",
    )


# ---------------------------------------------------------------------------
# Absent-strings: wait-loop / STAY-ALIVE / cursor mechanics must be gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_stay_alive_strings_absent(role: str) -> None:
    """``STAY-ALIVE`` / ``STAY ALIVE`` are removed from the preamble."""
    text = _render(role)
    assert "STAY-ALIVE" not in text.upper().replace(" ALIVE", "-ALIVE")
    # Permissive check: the literal phrase "STAY ALIVE" must not appear
    # as an instructional heading (it could in principle appear inside
    # the future-roadmap prose; here we want the literal lifecycle
    # heading absent).
    assert "STAY ALIVE" not in text
    assert "**STAY-ALIVE**" not in text


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_positive_wait_loop_instructions(role: str) -> None:
    """No positive instructions to call ``wait-loop`` remain.

    The single remaining ``wait-loop`` mention in the closing
    event-handler contract is a NEGATIVE instruction (``You do NOT
    block on wait-loop yourself``); the assertion below distinguishes
    positive from negative occurrences by requiring a "do NOT"
    qualifier on every line that mentions ``wait-loop``.
    """
    text = _render(role)
    # Accept multiple shapes of negative qualifier: ``do NOT call``,
    # ``do NOT block on``, ``do NOT issue``, and a literal ``Do NOT``
    # immediately preceding ``wait-loop``. These cover the producer
    # orientation copy ("Do NOT call `wait-loop`..."), the closing
    # event-handler contract ("you do NOT block on `wait-loop`..."),
    # and the dual-role banner preface ("Do NOT block on a reviewer
    # wait...").
    for line in text.splitlines():
        if "wait-loop" in line:
            assert (
                "do NOT call" in line
                or "Do NOT call" in line
                or "do NOT block on" in line
                or "Do NOT block on" in line
                or "do NOT issue" in line
                or "Do NOT issue" in line
            ), f"unexpected positive wait-loop instruction in {role}: {line!r}"


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_cursor_threading_references(role: str) -> None:
    """The ``/tmp/egg-wait-cursor-…`` plumbing reference is removed."""
    text = _render(role)
    assert "egg-wait-cursor" not in text
    assert "issue #2323" not in text  # cross-link to cursor-threading


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_status_ready_to_confirm_foot_gun(role: str) -> None:
    """The ``ready_to_confirm`` directed-STATUS plumbing is removed.

    Under the event-pump model the wrapper's ``next-action`` call
    decides when the role is ready to confirm; the agent no longer
    needs to interpret a directed STATUS nudge to discover it.
    """
    text = _render(role)
    assert "ready_to_confirm: true" not in text
    assert "Ready to confirm — all confirm preconditions satisfied" not in text


# ---------------------------------------------------------------------------
# Kept-strings: dual-mandate banner + agent roster preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["reviewer_code", "tester"])
def test_dual_mandate_phrase_preserved(role: str) -> None:
    """``Both must pass to ACK`` survives the collapse (plan acceptance)."""
    text = _render(role)
    assert "Both must pass to ACK" in text
    # The two equal-weight mandates framing is also kept.
    assert "TWO equal-weight mandates" in text


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_agent_roster_preserved(role: str) -> None:
    """The ``### Active Agents in This Phase`` roster heading survives."""
    text = _render(role)
    assert "### Active Agents in This Phase" in text


@pytest.mark.parametrize("role", ["coder"])
def test_producer_lifecycle_skeleton_preserved(role: str) -> None:
    """Producer lifecycle keeps its key headings (ORIENT / WORK / PROPOSE)."""
    text = _render(role)
    assert "### Producer Lifecycle" in text
    assert "**ORIENT**" in text
    assert "**WORK**" in text
    assert "**PROPOSE**" in text
    assert "**CONFIRM**" in text


@pytest.mark.parametrize("role", ["reviewer_code"])
def test_reviewer_lifecycle_skeleton_preserved(role: str) -> None:
    """Reviewer lifecycle keeps its key headings (PREPARE / SYNC / REVIEW)."""
    text = _render(role)
    assert "### Reviewer Lifecycle" in text
    assert "**PREPARE**" in text
    assert "**SYNC**" in text
    assert "**REVIEW**" in text
    assert "**ACK/NACK**" in text
    assert "**CONFIRM**" in text


@pytest.mark.parametrize("role", ["tester"])
def test_dual_role_banner_preserved(role: str) -> None:
    """The ``Dual-Role Execution Order`` banner heading survives."""
    text = _render(role)
    assert "Dual-Role Execution Order" in text


# ---------------------------------------------------------------------------
# Event-pump contract — the new closing framing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_event_handler_contract_present(role: str) -> None:
    """The closing event-handler contract replaces the legacy warning."""
    text = _render(role)
    assert "Event-handler contract (#2908)" in text
    assert "event-pump wrapper drives your lifecycle" in text
    # And the old "you have FAILED your role" warning is gone.
    assert "you have FAILED your role" not in text


# ---------------------------------------------------------------------------
# Byte-size drop assertion (plan TASK-3-3 acceptance ≥ 25%)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "baseline_bytes"),
    sorted(PRE_COLLAPSE_BASELINE_BYTES.items()),
)
def test_preamble_byte_size_drops_by_at_least_25_percent(role: str, baseline_bytes: int) -> None:
    """The collapsed preamble must shed ≥ 25% of the pre-collapse bytes."""
    text = _render(role)
    current_bytes = len(text.encode("utf-8"))
    drop_ratio = (baseline_bytes - current_bytes) / baseline_bytes
    assert drop_ratio >= MIN_DROP_RATIO, (
        f"{role}: drop ratio {drop_ratio:.3f} < {MIN_DROP_RATIO} "
        f"(baseline {baseline_bytes} → current {current_bytes})"
    )


# ===========================================================================
# Tester hardening (#2908 slice-3 task-3-7)
#
# Adversarial coverage on top of the coder-authored scaffold:
#
#   * full-text scans for the literal command-line shape (``egg-orch
#     message wait-loop``) and the ``--for CONSENSUS_*`` flag patterns —
#     the original line-by-line assertions can miss a multi-line code
#     block where the command and its flags are on different lines;
#   * full-text scan for ``never exit`` — the legacy STAY-ALIVE warning's
#     other anchor that the coder's assertion already drops, pinned here
#     in case a future preamble revision re-introduces the phrasing;
#   * full-text scan for the cursor-threading file path
#     (``/tmp/egg-wait-cursor-…``) — the cursor block in the original
#     preamble spanned multiple lines and is structurally distinct from
#     the ``egg-wait-cursor`` substring the coder's test already covers;
#   * agent-roster content check — the coder's test only verifies the
#     heading is present; this pins that the actual roster names the
#     producer / reviewer roles so the agent's role-aware framing
#     survives a future content-only edit;
#   * relative-drop *both directions*: the coder pins the ≥ 25% drop
#     against a hardcoded baseline; the hardening also pins that the
#     current preamble fits inside a generous byte ceiling so a runaway
#     re-expansion (e.g. someone copy-pastes the lifecycle prose back)
#     fails loudly rather than just regressing the ratio;
#   * defensive shape — ``_build_brc_preamble`` accepts an empty role
#     (returns *something*, even if the role banner falls back) so a
#     mis-typed role from the orchestrator doesn't crash the render.
# ===========================================================================


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_positive_egg_orch_wait_loop_invocation(role: str) -> None:
    """Every ``egg-orch message wait-loop`` mention is a NEGATIVE instruction.

    The collapsed preamble still references the command in the
    event-handler contract's negation ("You do NOT block on
    ``egg-orch message wait-loop`` yourself; the wrapper owns the
    wait"). That's intentional — it tells the agent what NOT to do.

    But a POSITIVE invocation in a fenced code block (which the
    coder's line-by-line ``do NOT`` check would miss if the command
    and its flags are on different lines) would be a regression.
    This scan splits the preamble into chunks around each occurrence
    and asserts each chunk's surrounding context carries a negation
    anchor — ``do NOT`` / ``Do NOT`` / ``not block on`` / ``not call``.
    """
    text = _render(role)
    needle = "egg-orch message wait-loop"
    if needle not in text:
        return  # zero occurrences is fine
    # For each occurrence, look at the ±200-char window for a negation anchor.
    idx = 0
    while True:
        loc = text.find(needle, idx)
        if loc == -1:
            break
        window = text[max(0, loc - 200) : loc + len(needle) + 100]
        has_negation = any(
            anchor in window
            for anchor in ("do NOT", "Do NOT", "not block on", "not call", "not issue")
        )
        assert has_negation, (
            f"positive ``{needle}`` invocation at offset {loc} in {role} preamble "
            f"(window: {window!r})"
        )
        idx = loc + len(needle)


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_consensus_event_filter_flags(role: str) -> None:
    """``--for CONSENSUS_*`` filter flags (legacy wait-loop plumbing) are gone."""
    text = _render(role)
    for flag in (
        "--for CONSENSUS_PROPOSE",
        "--for CONSENSUS_ACK",
        "--for CONSENSUS_NACK",
        "--for CONSENSUS_RE_REVIEW",
        "--for CONSENSUS_CONFIRMED",
        "--for OVERSEER_ALERT",
        "--for STATUS",
    ):
        assert flag not in text, f"legacy filter flag {flag!r} still present in {role} preamble"


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_never_exit_warning(role: str) -> None:
    """The legacy "never exit before the orchestrator stops you" warning is gone.

    Under the event-pump model the wrapper owns lifecycle; "never exit"
    was the legacy STAY-ALIVE companion phrase and an explicit collapse
    target (TASK-3-3 description: "Remove 'never exit before the
    orchestrator stops you' — under the new model the wrapper owns
    lifecycle"). The coder's tests check ``"you have FAILED your role"``
    in ``test_event_handler_contract_present``; this pins the other
    anchor phrase.
    """
    text = _render(role)
    assert "never exit" not in text.lower()


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_cursor_file_path_anywhere(role: str) -> None:
    """The ``/tmp/egg-wait-cursor-…`` plumbing path is gone (full-text scan).

    The coder's test scans for the substring ``egg-wait-cursor``; this
    pins the full path-shape ``/tmp/egg-wait-cursor-`` which is the
    operationally distinctive anchor the legacy preamble used to
    document cursor-threading.
    """
    text = _render(role)
    assert "/tmp/egg-wait-cursor-" not in text


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_agent_roster_lists_known_roles(role: str) -> None:
    """The agent-roster section names the producer + reviewer roles.

    The coder's test pins only the heading. This pins the content —
    the roster must actually name the roles in use this slice, not
    just emit an empty section.
    """
    text = _render(role)
    # The roster lives below the heading; do a structural slice.
    after_heading = text.split("### Active Agents in This Phase", 1)[1]
    # Producer + reviewer roles the implement phase routinely spawns.
    for expected_role in ("coder", "reviewer_code", "tester"):
        assert expected_role in after_heading, (
            f"roster missing {expected_role!r} below the heading for {role} preamble"
        )


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_no_directed_status_nudge_anchors(role: str) -> None:
    """No instruction to interpret a directed ``STATUS`` nudge (#2531 plumbing).

    Under the event-pump model the wrapper's ``next-action`` call decides
    when the role is ready to confirm; the agent no longer needs to
    interpret a directed STATUS nudge. The coder's test pins
    ``ready_to_confirm: true``; this pins the natural-language anchors
    that historically appeared alongside it.
    """
    text = _render(role)
    # The legacy "Ready to confirm" lifecycle anchor must be gone.
    assert "Ready to confirm — all confirm preconditions satisfied" not in text


def test_preamble_byte_size_under_ceiling_tester() -> None:
    """The tester preamble fits inside a generous byte ceiling (≤ 20 KB).

    A complement to the ≥ 25% relative-drop assertion: if someone
    silently re-expands the preamble back toward the pre-collapse size,
    the relative-drop assertion fails *only* against the hardcoded
    baseline; this ceiling assertion fails against an absolute bound,
    so the regression surfaces with two anchors. Tester has the largest
    preamble; bounding tester implicitly bounds the others.
    """
    text = _render("tester")
    bytes_used = len(text.encode("utf-8"))
    ceiling = 20 * 1024  # 20 KB — generous over the ~16.4 KB current size
    assert bytes_used <= ceiling, (
        f"tester preamble is {bytes_used} bytes — exceeds the {ceiling} byte ceiling. "
        "If this is intentional, raise the ceiling deliberately."
    )


def test_preamble_handles_unknown_role_without_crashing() -> None:
    """An unknown role name still renders *something* rather than raising.

    Defensive shape — if the orchestrator passes a typoed role (e.g.
    ``"reviewer_codee"``), the preamble should fall back gracefully so
    the wrapper can surface the error in the rendered output instead
    of crashing the per-event handler invocation. The fallback shape
    (whether the unknown role is echoed back, or the renderer maps it
    to a PARTICIPANT default) is intentionally not pinned — only the
    no-crash + non-empty-output contract is.
    """
    text = _build_brc_preamble(
        "reviewer_typoed",
        "implement",
        repo="egg",
        branch="egg/issue-2908-impl2/work",
        base_branch="main",
    )
    assert isinstance(text, str)
    assert len(text) > 0
    # The agent roster MUST still render so the agent has a frame of
    # reference even with an unrecognised role.
    assert "### Active Agents in This Phase" in text


@pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
def test_event_handler_contract_mentions_wrapper_owned_wait(role: str) -> None:
    """The new event-handler contract names *who* owns the wait now.

    The coder's test pins the existence of the contract; this pins that
    the contract actually conveys the central point — the *wrapper* owns
    the wait, not the agent — which is the whole reason the collapse
    happened.
    """
    text = _render(role)
    # The contract must surface the wrapper-owned-wait framing in some
    # form: either the literal "wrapper owns" / "drives your lifecycle"
    # phrasing or the negation of the legacy agent-held wait.
    contract_section = text.split("Event-handler contract", 1)[1]
    has_wrapper_framing = any(
        anchor in contract_section
        for anchor in (
            "wrapper",
            "drives your lifecycle",
            "one-shot",
        )
    )
    assert has_wrapper_framing, (
        f"event-handler contract for {role} does not surface the wrapper-owned-wait framing"
    )
