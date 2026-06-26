"""Slice-6 authority-plane contract tests (issue #2270, task-6-2).

Slice-6 adds the **bounded corrective vocabulary executor** (§4): the
orchestrator-side ``CorrectiveExecutor`` that *executes* a CLOSED set of three
corrective actions — ``nudge_agent``, ``respawn_cohort``, ``open_operator_hitl``.
The on-demand OVERSEER *adjudicator* (slice-4, ``overseer.decision_maker``) only
**advises** — it returns one of those recommendations plus ``none``; this plane
is the only thing that acts.

This module is the **tester contract** that pins the slice-6 production surface;
the coder reconciles ``overseer/corrective.py`` (and the wiring in
``routes/pipelines.py``) to it — the same tester-leads-coder flow used in slices
2–5. The assertions track the names, signatures and polarity the coder ships.

Production surface this contract pins
-------------------------------------

* ``overseer.corrective.CORRECTIVE_ACTIONS`` — the CLOSED vocabulary, exactly
  ``{"nudge_agent", "respawn_cohort", "open_operator_hitl"}``. ``none`` (the
  adjudicator's "false alarm / no action") is deliberately NOT executable.

* ``overseer.corrective.CorrectiveExecutor`` — dependency-injected so the unit
  test drives it with spies and the real wiring lives in ``routes/pipelines``:

      CorrectiveExecutor(
          *,
          open_operator_hitl,      # Callable[..., str]  orchestrator-identity
                                   #   contract-decision writer → decision_id.
                                   #   This is the REAL enforcement point
                                   #   (gateway/agent_restrictions + contract
                                   #   RBAC), NOT an agent gateway path.
          nudge_agent,             # Callable[..., bool] wraps
                                   #   routes.pipelines._send_brc_confirmation_nudge
          respawn_cohort,          # Callable[..., bool] general restart machinery
          audit_sink=None,         # Callable[[dict], None] — every attempt logged
          max_actions_per_window=..., window_seconds=..., clock=...,
      )

  ``execute(action, *, pipeline_id, running_agent_count=1, phase=None,
  target_role=None, finding=None, idempotency_key=None, question=None,
  options=None) -> CorrectiveOutcome`` is the single entry point. The injected
  dependencies are invoked with **keyword arguments** carrying at least
  ``pipeline_id`` (plus ``target_role`` for nudge/respawn and ``question`` for
  the HITL writer).

* ``overseer.corrective.CorrectiveOutcome`` — an immutable result carrying
  ``action`` / ``status`` / ``executed`` (bool). ``status`` is one of
  ``executed | denied | barred | rate_limited | deduplicated``.

Decision precedence inside ``execute`` (documented so the assertions below stay
independent — each test exercises exactly one gate):
  1. action ∉ vocabulary            → ``denied``        (unauthorized stays denied)
  2. zero-agent HITL park            → ``barred``        (nothing to correct)
  3. duplicate idempotency key       → ``deduplicated``  (at-most-once)
  4. rate-limit window exceeded      → ``rate_limited``
  5. otherwise                       → ``executed``
Every branch appends an audit record.

Skip→strict convention
-----------------------

This slice is **additive**, so the integration sentinel is the importability of
``overseer.corrective`` itself: while the coder's module is absent each
executor row **skips** (so the suite is green on the tester's standalone
branch); the moment the module lands, a still-missing pinned symbol becomes a
**loud failure** (``_require``), never a silent skip. The adjudicator-advises
rows assert ``overseer.decision_maker`` (which exists today) **strictly now** —
they are regression guards that execution must never leak into the advisor.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup + docker stubs (mirrors test_overseer_lifecycle.py so the overseer
# package imports without the real docker SDK present).
# ---------------------------------------------------------------------------

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# The advisor module exists today (slice-4) — its contract is asserted strictly.
try:
    from overseer.decision_maker import ADJUDICATION_ACTIONS, AdjudicationVerdict
except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - import guard
    pytest.skip(
        f"overseer.decision_maker not importable yet: {exc}",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Slice-6 integration sentinel + hardened skip→strict guard
# ---------------------------------------------------------------------------

# Candidate homes for the executor. The coder's contract task names
# ``overseer/corrective.py``; the fallback guards against a near-miss rename so a
# wrong-surface change fails loudly instead of skipping forever.
_CORRECTIVE_MODULES = ("overseer.corrective", "overseer.corrective_executor")

# The executable vocabulary this contract pins (mirrors the architect's §4
# closed set and ``ADJUDICATION_ACTIONS`` minus the non-executable ``none``).
_EXPECTED_ACTIONS = frozenset({"nudge_agent", "respawn_cohort", "open_operator_hitl"})


def _corrective_module():
    """Return the imported executor module, or ``None`` if not landed yet."""
    for name in _CORRECTIVE_MODULES:
        try:
            return __import__(name, fromlist=["_"])
        except ImportError, ModuleNotFoundError:
            continue
    return None


def _slice6_landed() -> bool:
    """True once the coder's slice-6 executor module is importable."""
    return _corrective_module() is not None


def _require_module():
    """Resolve the executor module under the skip→strict convention.

    Absent → ``pytest.skip`` (green on the tester's standalone branch before the
    coder reconciles). The moment it lands every row below turns strict.
    """
    module = _corrective_module()
    if module is None:
        pytest.skip("overseer.corrective not landed by the coder yet — strict at integration")
    return module


def _require(obj: object, name: str):
    """Resolve a slice-6 surface; fail loudly if the module landed but the name
    is absent (wrong-surface regression), else skip."""
    if hasattr(obj, name):
        return getattr(obj, name)
    if _slice6_landed():
        pytest.fail(
            f"{name} is absent though the slice-6 executor module has landed — "
            "wrong-surface regression: the delivered name/signature diverged "
            "from this contract (#2270 §4)."
        )
    pytest.skip(f"{name} not landed by the coder yet — strict at integration")


# ---------------------------------------------------------------------------
# Test harness: a controllable clock + a spy-wired executor factory.
# ---------------------------------------------------------------------------


class _Clock:
    """A manually-advanced monotonic clock for deterministic rate-limit tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_executor(
    *,
    max_actions_per_window: int = 100,
    window_seconds: float = 60.0,
    clock: _Clock | None = None,
):
    """Construct a ``CorrectiveExecutor`` wired to spies + an audit list.

    Returns ``(executor, spies, audit)`` where ``spies`` is a dict of the three
    injected MagicMocks and ``audit`` is the list the ``audit_sink`` appends to.
    """
    module = _require_module()
    CorrectiveExecutor = _require(module, "CorrectiveExecutor")

    spies = {
        "open_operator_hitl": MagicMock(return_value="decision-7"),
        "nudge_agent": MagicMock(return_value=True),
        "respawn_cohort": MagicMock(return_value=True),
    }
    audit: list[dict] = []

    executor = CorrectiveExecutor(
        open_operator_hitl=spies["open_operator_hitl"],
        nudge_agent=spies["nudge_agent"],
        respawn_cohort=spies["respawn_cohort"],
        audit_sink=audit.append,
        max_actions_per_window=max_actions_per_window,
        window_seconds=window_seconds,
        clock=clock or _Clock(),
    )
    return executor, spies, audit


# ===========================================================================
# task-6-2 — the CLOSED vocabulary: exactly three executable actions
# ===========================================================================


class TestClosedVocabulary:
    """The executor exposes EXACTLY the three corrective actions, no more."""

    def test_corrective_actions_constant(self) -> None:
        module = _require_module()
        actions = _require(module, "CORRECTIVE_ACTIONS")
        assert set(actions) == set(_EXPECTED_ACTIONS), (
            "CORRECTIVE_ACTIONS must be exactly the closed §4 vocabulary "
            "{nudge_agent, respawn_cohort, open_operator_hitl}"
        )

    def test_none_is_not_executable(self) -> None:
        """``none`` is the adjudicator's no-op; it is NOT an executable action."""
        module = _require_module()
        actions = _require(module, "CORRECTIVE_ACTIONS")
        assert "none" not in set(actions)

    def test_executor_advertises_exactly_three(self) -> None:
        executor, _, _ = _make_executor()
        advertised = getattr(executor, "ACTIONS", None) or getattr(executor, "actions", None)
        assert advertised is not None, "executor must advertise its action vocabulary"
        assert set(advertised) == set(_EXPECTED_ACTIONS)


# ===========================================================================
# task-6-2 — allow: each action routes to its injected dependency (executed)
# ===========================================================================


class TestActionsExecute:
    """A permitted action with agents running runs its injected side-effect."""

    def test_nudge_agent_routes_to_injected_nudge(self) -> None:
        executor, spies, _ = _make_executor()
        outcome = executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            phase="implement",
        )
        assert outcome.executed is True
        assert outcome.status == "executed"
        spies["nudge_agent"].assert_called_once()
        spies["respawn_cohort"].assert_not_called()
        spies["open_operator_hitl"].assert_not_called()
        kwargs = spies["nudge_agent"].call_args.kwargs
        assert kwargs.get("pipeline_id") == "pipe-6"
        assert kwargs.get("target_role") == "coder"

    def test_respawn_cohort_routes_to_injected_respawn(self) -> None:
        executor, spies, _ = _make_executor()
        outcome = executor.execute(
            "respawn_cohort",
            pipeline_id="pipe-6",
            running_agent_count=1,
            target_role="reviewer_code",
        )
        assert outcome.executed is True
        assert outcome.status == "executed"
        spies["respawn_cohort"].assert_called_once()
        spies["nudge_agent"].assert_not_called()
        kwargs = spies["respawn_cohort"].call_args.kwargs
        assert kwargs.get("pipeline_id") == "pipe-6"
        assert kwargs.get("target_role") == "reviewer_code"

    def test_open_operator_hitl_routes_to_authorized_writer(self) -> None:
        """``open_operator_hitl`` creates the operator HITL via the injected
        orchestrator-identity contract-decision writer — never an agent path."""
        executor, spies, _ = _make_executor()
        outcome = executor.execute(
            "open_operator_hitl",
            pipeline_id="pipe-6",
            running_agent_count=1,
            question="Phase wedged — nudge, respawn, or abort?",
            options=["nudge", "respawn", "abort"],
        )
        assert outcome.executed is True
        assert outcome.status == "executed"
        spies["open_operator_hitl"].assert_called_once()
        spies["nudge_agent"].assert_not_called()
        spies["respawn_cohort"].assert_not_called()
        kwargs = spies["open_operator_hitl"].call_args.kwargs
        assert kwargs.get("pipeline_id") == "pipe-6"
        assert "question" in kwargs


# ===========================================================================
# task-6-2 — deny: anything outside the closed vocabulary stays denied
# ===========================================================================


class TestUnauthorizedDenied:
    """Unauthorized / out-of-vocabulary actions are denied with no side-effect."""

    @pytest.mark.parametrize(
        "action",
        ["none", "delete_repo", "force_merge", "shutdown_pipeline", "", "NUDGE_AGENT"],
    )
    def test_out_of_vocabulary_denied(self, action: str) -> None:
        executor, spies, _ = _make_executor()
        outcome = executor.execute(action, pipeline_id="pipe-6", running_agent_count=2)
        assert outcome.executed is False
        assert outcome.status == "denied"
        for spy in spies.values():
            spy.assert_not_called()


# ===========================================================================
# task-6-2 — barred during a zero-agent HITL park (the §4 guarantee)
# ===========================================================================


class TestBarredDuringZeroAgentPark:
    """No corrective action fires when no agents are running (zero-agent park).

    Mirrors slice-5's ``_overseer_should_be_present`` rule: with zero agents in
    flight there is nothing to nudge or respawn and a human is already in the
    loop, so the executor bars every action.
    """

    @pytest.mark.parametrize("action", sorted(_EXPECTED_ACTIONS))
    def test_zero_agents_bars_every_action(self, action: str) -> None:
        executor, spies, _ = _make_executor()
        outcome = executor.execute(action, pipeline_id="pipe-6", running_agent_count=0)
        assert outcome.executed is False
        assert outcome.status == "barred"
        for spy in spies.values():
            spy.assert_not_called()


# ===========================================================================
# task-6-2 — idempotency: at-most-once per idempotency key
# ===========================================================================


class TestIdempotency:
    """A repeated idempotency key executes the side-effect at most once."""

    def test_duplicate_key_deduplicated(self) -> None:
        executor, spies, _ = _make_executor()
        first = executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            idempotency_key="stall-coder-1",
        )
        second = executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            idempotency_key="stall-coder-1",
        )
        assert first.executed is True
        assert second.executed is False
        assert second.status == "deduplicated"
        spies["nudge_agent"].assert_called_once()

    def test_distinct_keys_each_execute(self) -> None:
        executor, spies, _ = _make_executor()
        executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            idempotency_key="stall-coder-1",
        )
        executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            idempotency_key="stall-coder-2",
        )
        assert spies["nudge_agent"].call_count == 2


# ===========================================================================
# task-6-2 — rate limiting: a bounded number of actions per window
# ===========================================================================


class TestRateLimiting:
    """Actions are rate-limited; the window is enforced via the injected clock."""

    def test_exceeding_window_is_rate_limited(self) -> None:
        clock = _Clock()
        executor, spies, _ = _make_executor(
            max_actions_per_window=1, window_seconds=60.0, clock=clock
        )
        first = executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            idempotency_key="a",
        )
        second = executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            idempotency_key="b",
        )
        assert first.executed is True
        assert second.executed is False
        assert second.status == "rate_limited"
        spies["nudge_agent"].assert_called_once()

    def test_budget_recovers_after_window(self) -> None:
        clock = _Clock()
        executor, spies, _ = _make_executor(
            max_actions_per_window=1, window_seconds=60.0, clock=clock
        )
        executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            idempotency_key="a",
        )
        clock.advance(61.0)  # roll past the window
        recovered = executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
            idempotency_key="b",
        )
        assert recovered.executed is True
        assert recovered.status == "executed"
        assert spies["nudge_agent"].call_count == 2


# ===========================================================================
# task-6-2 — audit logging: every attempt is recorded
# ===========================================================================


class TestAuditLogging:
    """Every execute attempt — executed, denied, or barred — is audit-logged."""

    def test_executed_action_is_audited(self) -> None:
        executor, _, audit = _make_executor()
        executor.execute(
            "nudge_agent",
            pipeline_id="pipe-6",
            running_agent_count=2,
            target_role="coder",
        )
        assert len(audit) == 1
        record = audit[0]
        assert record.get("action") == "nudge_agent"
        assert record.get("status") == "executed"
        assert record.get("pipeline_id") == "pipe-6"

    def test_denied_and_barred_attempts_are_audited(self) -> None:
        executor, _, audit = _make_executor()
        executor.execute("force_merge", pipeline_id="pipe-6", running_agent_count=2)
        executor.execute("nudge_agent", pipeline_id="pipe-6", running_agent_count=0)
        assert len(audit) == 2
        statuses = {r.get("status") for r in audit}
        assert statuses == {"denied", "barred"}


# ===========================================================================
# task-6-2 — the adjudicator ONLY advises (execution lives in this plane)
# ===========================================================================


class TestAdjudicatorOnlyAdvises:
    """``overseer.decision_maker`` advises; it must never execute. (strict now)"""

    def test_advisory_vocabulary_is_executable_plus_none(self) -> None:
        """The advisor's vocabulary is the executable set plus the no-op ``none``."""
        assert set(ADJUDICATION_ACTIONS) == set(_EXPECTED_ACTIONS) | {"none"}

    def test_verdict_cannot_act(self) -> None:
        """The verdict is a pure dataclass — it carries a recommendation, not an
        executor handle. No ``execute``/``apply`` method may exist on it."""
        assert not hasattr(AdjudicationVerdict, "execute")
        assert not hasattr(AdjudicationVerdict, "apply")

    def test_advisor_module_does_not_execute(self) -> None:
        """The advisor module never constructs/executes the authority plane —
        keeping ADVISE and EXECUTE in separate modules (#2270 §4)."""
        import overseer.decision_maker as dm

        source = Path(dm.__file__).read_text(encoding="utf-8")
        assert "class CorrectiveExecutor" not in source, (
            "the CorrectiveExecutor must live in overseer/corrective.py, not in "
            "the advisor — the adjudicator only advises"
        )
        assert "CorrectiveExecutor(" not in source, (
            "the advisor must not instantiate/execute the authority plane"
        )

    def test_executable_vocabulary_matches_advisory_minus_none(self) -> None:
        """Once the executor lands, its vocabulary == advisory actions − none."""
        module = _require_module()
        actions = _require(module, "CORRECTIVE_ACTIONS")
        assert set(actions) == set(ADJUDICATION_ACTIONS) - {"none"}
