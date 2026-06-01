"""Snapshot test for the collapsed BRC preamble (TASK-3-3 / TASK-3-7).

Per ``task-3-3`` of the slice-3 contract (#2908), the coder collapses
``_build_brc_preamble`` in ``orchestrator/routes/pipelines.py``
deleting the STAY-ALIVE / wait-loop mechanics / cursor-threading /
pre-confirm-wait foot-gun guidance (Producer Lifecycle step 4
wait-loop plumbing; Producer step 6 STAY-ALIVE loop;
``--since`` / cursor guidance).

What survives the collapse:

* Agent roster (the active agents enumeration)
* Reviewer / producer assignments
* Dual-Role Execution Order banner (#2749)
* Dual-mandate adversarial re-review banner
  (``pipelines.py:12849-12872`` pre-collapse; the
  ``"Both must pass to ACK"`` phrase verified at lines 12856-12857)

What this test enforces, per the task-3-7 acceptance criteria:

  (a) the new collapsed snapshot matches (regression gate on the post-
      collapse content for each of the three caller sites in
      ``pipelines.py``)
  (b) ``STAY-ALIVE`` / ``wait-loop`` / ``cursor`` strings are absent
  (c) the agent roster is present
  (d) preamble byte size drops by ≥ 25 % vs the pre-collapse baseline
      with a 5 % tolerance band (task-3-3 softened the original
      ≥ 40 % target per reviewer_plan v2 non-blocker; task-3-7
      acceptance text was authored against the pre-softening target,
      so we use the task-3-3 figure here as the authoritative one)

  Plus the WS7 cache-measurement file capture under
  ``.egg-state/agent-outputs/ws7-measurement-slice-3.json``.

The pre-collapse byte baselines were captured against the
``_build_brc_preamble`` output on the current branch *before* the
collapse landed (see ``PRE_COLLAPSE_BYTES`` below). If the coder
re-measures and finds a different baseline, the constant should be
updated in lock-step with the collapse commit — the assertion is a
regression gate on a stable artefact, not on the test fixture itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Mock heavy dependencies that pipelines.py imports at module level.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

_project_root = Path(__file__).parent.parent.parent
for _p in (_project_root / "orchestrator", _project_root / "shared"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from routes.pipelines import _build_brc_preamble  # noqa: E402

# Pre-collapse byte baselines, measured against ``_build_brc_preamble``
# on this branch *before* task-3-3 landed. These reflect the current
# function output for the representative role at each of the three
# caller sites in ``orchestrator/routes/pipelines.py``::
#
#   site 1 (pipelines.py:13574 pre-collapse) — coder / refiner path:
#       ``base_prompt += _build_brc_preamble(role_value=…, …)``
#   site 2 (pipelines.py:13607 pre-collapse) — reviewer_* path:
#       ``review_prompt += "\n" + _build_brc_preamble(role_value=…, …)``
#   site 3 (pipelines.py:13635 pre-collapse) — generic role path:
#       ``lines.append(_build_brc_preamble(role_value=…, …))``
#
# Each entry maps the representative role to the measured byte size
# of the pre-collapse preamble. Captured on
# ``egg/issue-2908-impl2-slice-3-tester/work`` at HEAD prior to the
# collapse commit:
#
#   $ python3 -c "from routes.pipelines import _build_brc_preamble; \
#       print(len(_build_brc_preamble('coder', 'implement', \
#       repo='jwbron/egg', branch='egg/issue-2908-impl2/slice-3')))"
#   9445
#
# coder is 9445, reviewer_code is 12451, tester is 22009 — taken
# verbatim from the above.
PRE_COLLAPSE_BYTES: dict[str, int] = {
    "coder": 9445,
    "reviewer_code": 12451,
    "tester": 22009,
}

# Authoritative collapse target from task-3-3 (softened from ≥ 40 %
# per reviewer_plan v2 non-blocker). Task-3-7 acceptance asks for a
# 5 % tolerance band; the assertion below uses ``required_drop_pct *
# (1 - tolerance)`` as the effective floor so a small fluctuation in
# the surviving content doesn't false-fail the gate.
REQUIRED_DROP_PCT = 0.25  # ≥ 25 % size drop
TOLERANCE_PCT = 0.05  # ± 5 % wobble allowed

# Strings that must NOT survive the collapse — these are precisely the
# seams the slice-3 design exists to remove. The task-3-3 acceptance
# names ``STAY-ALIVE / wait-loop / cursor`` with the hyphenated heading
# but the existing preamble uses ``STAY ALIVE`` (with a space) as the
# Producer step-6 heading; we match against both forms so a partial
# collapse that swaps the spacing does not slip through.
ABSENT_AFTER_COLLAPSE: tuple[str, ...] = (
    "STAY-ALIVE",
    "STAY ALIVE",
    "wait-loop",
    "Cursor threading",
    "--since",
    "egg-wait-cursor",
)

# Strings that MUST survive the collapse — the roster, the dual-role
# ordering banner (#2749), and the dual-mandate adversarial re-review
# banner carry behavioural framing slice-3 explicitly keeps. The
# dual-role ordering banner is rendered only when the agent is both a
# producer and a reviewer (today: tester); the dual-mandate banner is
# rendered for any agent that has a reviewer role (reviewer_* and
# dual-role); the roster renders for every concurrent preamble caller.
SURVIVES_COLLAPSE_DUAL_ROLE: tuple[str, ...] = (
    "Active Agents",  # roster heading
    "Dual-Role Execution Order",  # dual-role ordering banner
    "Both must pass to ACK",  # dual-mandate banner phrase
)

SURVIVES_COLLAPSE_REVIEWER: tuple[str, ...] = (
    "Active Agents",
    "Both must pass to ACK",
)

SURVIVES_COLLAPSE_PRODUCER: tuple[str, ...] = ("Active Agents",)


def _render_preamble(role: str) -> str:
    """Render the preamble for a representative role on this branch.

    Branch + base_branch values are pinned so the snapshot is stable
    across runs.
    """
    return _build_brc_preamble(
        role_value=role,
        phase="implement",
        repo="jwbron/egg",
        branch="egg/issue-2908-impl2/slice-3",
        base_branch="main",
    )


# ---------------------------------------------------------------------------
# Absent-strings gate — the seams the collapse exists to remove
# ---------------------------------------------------------------------------


class TestAbsentStringsAfterCollapse:
    """The collapse must remove the wait-loop / STAY-ALIVE / cursor seams."""

    @pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
    @pytest.mark.parametrize("forbidden", ABSENT_AFTER_COLLAPSE)
    def test_forbidden_strings_absent(self, role: str, forbidden: str):
        preamble = _render_preamble(role)
        assert forbidden not in preamble, (
            f"`{forbidden}` survived the collapse in the {role} preamble. "
            "The slice-3 collapse exists to remove the wait-loop / "
            "STAY-ALIVE / cursor seam — see task-3-3 of #2908. "
            "If you intentionally re-introduced this guidance, update "
            "ABSENT_AFTER_COLLAPSE in this test together with the "
            "collapse rollback commit."
        )


# ---------------------------------------------------------------------------
# Surviving-content gate — what the collapse explicitly preserves
# ---------------------------------------------------------------------------


class TestSurvivingContentAfterCollapse:
    """Roster + dual-role + dual-mandate framing must survive the collapse."""

    def test_dual_role_preamble_keeps_roster_and_banners(self):
        preamble = _render_preamble("tester")
        for survivor in SURVIVES_COLLAPSE_DUAL_ROLE:
            assert survivor in preamble, (
                f"`{survivor}` was lost in the collapse for the tester "
                "(dual-role) preamble — task-3-3 mandates that the "
                "dual-role ordering banner and the dual-mandate "
                'adversarial re-review banner ("Both must pass to ACK") '
                "stay."
            )

    def test_reviewer_preamble_keeps_dual_mandate_banner(self):
        preamble = _render_preamble("reviewer_code")
        for survivor in SURVIVES_COLLAPSE_REVIEWER:
            assert survivor in preamble, (
                f"`{survivor}` was lost in the collapse for the "
                f"reviewer_code preamble — task-3-3 mandates the "
                'dual-mandate banner ("Both must pass to ACK") stays.'
            )

    def test_producer_preamble_keeps_roster(self):
        preamble = _render_preamble("coder")
        for survivor in SURVIVES_COLLAPSE_PRODUCER:
            assert survivor in preamble, (
                f"`{survivor}` was lost in the collapse for the coder "
                "preamble — task-3-3 mandates the roster stays."
            )


# ---------------------------------------------------------------------------
# Byte-size collapse gate
# ---------------------------------------------------------------------------


class TestByteSizeDrop:
    """Post-collapse preamble must shrink by ≥ 25 % vs pre-collapse."""

    @pytest.mark.parametrize(
        ("role", "baseline_bytes"),
        sorted(PRE_COLLAPSE_BYTES.items()),
    )
    def test_byte_size_drop_meets_target(self, role: str, baseline_bytes: int):
        post = len(_render_preamble(role))
        drop_pct = (baseline_bytes - post) / baseline_bytes
        # Effective floor with the 5 % tolerance band — softens a small
        # wobble in surviving content so the gate doesn't flap on
        # editorial polish.
        floor_pct = REQUIRED_DROP_PCT * (1.0 - TOLERANCE_PCT)
        assert drop_pct >= floor_pct, (
            f"{role} preamble shrank by only {drop_pct:.1%} "
            f"({baseline_bytes} → {post} bytes); task-3-3 mandates a "
            f"≥ {REQUIRED_DROP_PCT:.0%} drop (effective floor "
            f"{floor_pct:.1%} with the {TOLERANCE_PCT:.0%} tolerance "
            "band). If the collapse intentionally kept more content, "
            "update PRE_COLLAPSE_BYTES in this test with a fresh "
            "measurement together with the collapse commit."
        )


# ---------------------------------------------------------------------------
# Snapshot stability — fixture round-trip for each caller-site role
# ---------------------------------------------------------------------------


SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "brc_preamble_collapsed"


def _snapshot_path(role: str) -> Path:
    return SNAPSHOT_DIR / f"{role}.txt"


class TestSnapshotMatches:
    """Post-collapse content is stable across runs.

    Snapshots are kept under
    ``orchestrator/tests/snapshots/brc_preamble_collapsed/<role>.txt``.
    The snapshot files are checked into git alongside the collapse
    commit; subsequent changes to ``_build_brc_preamble`` that mutate
    the surviving content surface here, NOT downstream in agent
    behaviour where they're harder to spot.

    To update a snapshot, delete the corresponding file and re-run
    the test — the first run after deletion writes a fresh snapshot.
    """

    @pytest.mark.parametrize("role", ["coder", "reviewer_code", "tester"])
    def test_snapshot_round_trips(self, role: str):
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _snapshot_path(role)
        rendered = _render_preamble(role)
        baseline = PRE_COLLAPSE_BYTES.get(role)
        drop_pct = (baseline - len(rendered)) / baseline if baseline else 0.0
        floor_pct = REQUIRED_DROP_PCT * (1.0 - TOLERANCE_PCT)

        if not path.exists():
            # First run after a delete — only record a fresh snapshot
            # once the byte-size gate has actually passed. Recording a
            # pre-collapse render would freeze the regression gate at
            # the wrong baseline and silently let the collapse skip.
            if baseline is not None and drop_pct < floor_pct:
                pytest.fail(
                    f"Refusing to record snapshot at "
                    f"{path.relative_to(_project_root)}: preamble for "
                    f"{role} has not yet shrunk past the floor "
                    f"({drop_pct:.1%} < {floor_pct:.1%}). Land the "
                    "task-3-3 collapse first, then re-run this test "
                    "to record the post-collapse snapshot."
                )
            path.write_text(rendered, encoding="utf-8")
            pytest.skip(
                f"Snapshot recorded at {path.relative_to(_project_root)}; "
                "commit alongside the collapse commit and re-run."
            )
        expected = path.read_text(encoding="utf-8")
        assert rendered == expected, (
            f"Preamble for {role} drifted from "
            f"{path.relative_to(_project_root)}. If the drift is "
            "intentional, delete the snapshot file and re-run to "
            "record a fresh one."
        )


# ---------------------------------------------------------------------------
# WS7 cache measurement file capture
# ---------------------------------------------------------------------------


WS7_MEASUREMENT_PATH = (
    _project_root / ".egg-state" / "agent-outputs" / "ws7-measurement-slice-3.json"
)


WS7_SCHEMA_REQUIRED_TOP_LEVEL: tuple[str, ...] = (
    "slice_id",
    "pipeline_id",
    "captured_at",
    "per_event",
    "total_invocations",
    "qwen_aggregate",
)


class TestWS7MeasurementFile:
    """``.egg-state/agent-outputs/ws7-measurement-slice-3.json`` exists
    with the documented schema.

    Per task-3-7 acceptance: ``WS7 measurement file captured under
    .egg-state/agent-outputs/ with the documented schema (per-event
    cache_read_input_tokens, total invocation count, Qwen aggregate)``.

    The actual ``cache_read_input_tokens`` numbers are filled in at
    flag-flip time (slice-4) when the event-pump runs end-to-end on a
    live pipeline; slice-3 ships the file with the schema in place so
    slice-4's comparison harness has a stable input to read against.
    """

    def test_ws7_measurement_file_exists(self):
        assert WS7_MEASUREMENT_PATH.exists(), (
            f"WS7 cache-measurement file missing at "
            f"{WS7_MEASUREMENT_PATH.relative_to(_project_root)}. "
            "Slice-3 must ship this file with the documented schema "
            "so slice-4 can compare cache reads against the "
            "pre-collapse baseline (task-3-7 acceptance criteria)."
        )

    def test_ws7_measurement_schema_present(self):
        payload = json.loads(WS7_MEASUREMENT_PATH.read_text(encoding="utf-8"))
        missing = [key for key in WS7_SCHEMA_REQUIRED_TOP_LEVEL if key not in payload]
        assert not missing, (
            f"WS7 measurement file missing required schema keys: "
            f"{missing}. Required top-level keys: "
            f"{WS7_SCHEMA_REQUIRED_TOP_LEVEL}."
        )

    def test_ws7_per_event_records_cache_reads(self):
        payload = json.loads(WS7_MEASUREMENT_PATH.read_text(encoding="utf-8"))
        per_event = payload.get("per_event", [])
        assert isinstance(per_event, list), (
            "WS7 ``per_event`` must be a list of records — slice-4's "
            "comparison harness reads it positionally."
        )
        # Each per-event record must carry cache_read_input_tokens —
        # even if the value is a placeholder zero today, the slot must
        # exist so slice-4 can populate it without a schema migration.
        for idx, record in enumerate(per_event):
            assert "cache_read_input_tokens" in record, (
                f"WS7 per_event[{idx}] missing ``cache_read_input_tokens`` "
                "— required per task-3-7 acceptance."
            )
