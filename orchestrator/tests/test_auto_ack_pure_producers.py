"""Tests for the pure-producer auto-ACK seed (#2581).

Covers ``ApprovalMatrix.seed_auto_ack_for_empty_pure_producers`` and the
delegating ``PeerConsensusTracker.seed_auto_ack_for_empty_pure_producers``
wrapper. The seed exists to prevent BRC consensus deadlock for slices
whose plan omits a producer role (e.g. a tester-only or documenter-only
slice): without it, CODER spawns with no work, its critical reviewers
have nothing to review, and consensus stalls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from approval_matrix import ApprovalMatrix, ApprovalState
from peer_consensus import PeerConsensusTracker
from review_graph import get_default_implement_graph


@pytest.fixture
def implement_graph():
    return get_default_implement_graph()


@pytest.fixture
def matrix(implement_graph):
    return ApprovalMatrix(implement_graph)


class TestSeedAutoAckEmptyPureProducers:
    """Direct tests against ``ApprovalMatrix``."""

    def test_documenter_only_slice_auto_acks_coder(self, matrix):
        """A documenter-only slice should auto-ACK CODER so its reviewers
        don't deadlock on an empty proposal."""
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert auto_acked == ["coder"]
        # CODER's consensus is fully satisfied after seeding — every
        # critical reviewer (including TESTER) has a seeded ACK at v1.
        assert matrix.is_fully_acked("coder") is True

    def test_tester_only_slice_auto_acks_coder_and_documenter(self, matrix):
        """A tester-only slice has neither a coder nor a documenter task —
        both pure producers should auto-ACK."""
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers({"tester"})
        assert auto_acked == ["coder", "documenter"]
        assert matrix.is_fully_acked("coder") is True
        # DOCUMENTER has no critical reviewers in the default implement
        # graph, so ``is_fully_acked`` returns True once any proposal is
        # recorded.
        assert matrix.is_fully_acked("documenter") is True

    def test_coder_only_slice_auto_acks_only_documenter(self, matrix):
        """A coder-only slice: DOCUMENTER (pure producer) auto-ACKs;
        TESTER is dual-role and is intentionally skipped so its reviewer
        responsibility for CODER stays active."""
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers({"coder"})
        assert auto_acked == ["documenter"]
        assert matrix.is_fully_acked("documenter") is True
        # TESTER not auto-ACKed — its producer-side proposal version is
        # still 0, so it isn't fully-ACKed.
        assert matrix.get_proposal_version("tester") == 0
        assert matrix.is_fully_acked("tester") is False

    def test_all_producers_present_is_noop(self, matrix):
        """When every producer has at least one task, seeding is a no-op."""
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers(
            {"coder", "tester", "documenter"}
        )
        assert auto_acked == []
        assert matrix.get_proposal_version("coder") == 0
        assert matrix.get_proposal_version("documenter") == 0

    def test_seeded_acks_carry_critical_reviewers(self, matrix):
        """The seeded proposal must satisfy every critical reviewer of
        the auto-ACKed producer, including dual-role reviewers like
        TESTER — that's what makes the empty proposal actually reach
        consensus instead of just being recorded."""
        matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        graph = matrix._graph
        for reviewer in graph.critical_reviewers_for("coder"):
            entry = matrix.get_entry(reviewer, "coder")
            assert entry is not None
            assert entry.state == ApprovalState.ACKED
            assert entry.version == 1

    def test_dual_role_producer_is_never_auto_acked(self, matrix):
        """Even when TESTER has no tasks (documenter-only slice), it must
        not be auto-ACKed as a producer — its dual role means it should
        always run so it can ACK/NACK CODER for real."""
        matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        # TESTER's producer-side state: untouched.
        assert matrix.get_proposal_version("tester") == 0

    def test_dual_role_reviewer_can_nack_to_override_seeded_ack(self, matrix):
        """The 'tester may need coder to do some work' recovery path:
        TESTER's seeded ACK of CODER is a starting state, not a verdict.
        If TESTER's producer work later uncovers a need for code, it can
        NACK at the seeded version and force CODER to re-propose."""
        matrix.seed_auto_ack_for_empty_pure_producers({"tester"})
        assert matrix.is_fully_acked("coder") is True

        matrix.record_nack("tester", "coder", version=1, reason="need helper module")

        assert matrix.is_fully_acked("coder") is False
        entry = matrix.get_entry("tester", "coder")
        assert entry is not None
        assert entry.state == ApprovalState.NACKED

    def test_real_proposal_supersedes_seeded_acks(self, matrix):
        """If the producer's container later proposes for real (version
        bump), the seeded version-1 ACKs are no longer at the latest
        version — the normal flow re-acquires fresh ACKs at v2."""
        matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert matrix.get_proposal_version("coder") == 1

        new_version = matrix.record_proposal("coder")
        assert new_version == 2
        # is_fully_acked falls back to False because seeded ACKs are at v1.
        assert matrix.is_fully_acked("coder") is False

    def test_seed_called_twice_keeps_consensus_reachable_but_bumps_version(self, matrix):
        """Calling the seeder twice with the same task set bumps the
        proposal version each time and re-records ACKs at the new
        version.

        The seeder is NOT idempotent on the matrix — each call advances
        ``proposal_version`` (v1 → v2 → …) and records fresh ACKs at
        that version. The post-state is still fully-ACKed (consensus
        reachable), but the version inflation matters: if a retry path
        ever calls the seeder while a real propose / ACK is in flight,
        seeded-at-v=N ACKs would invalidate any earlier verdicts at
        v<N. ``ConcurrentPhaseExecutor.spawn_specific_roles`` (the
        spawn-retry path) deliberately does NOT call the seeder again
        precisely to avoid that.
        """
        first = matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        second = matrix.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert first == ["coder"]
        assert second == ["coder"]
        # Version inflation is observable — not idempotent at the matrix
        # level even though the "consensus reachable" effect persists.
        assert matrix.get_proposal_version("coder") == 2
        assert matrix.is_fully_acked("coder") is True

    def test_seed_uses_public_producer_roles_accessor(self, implement_graph):
        """The seeder iterates ``graph.producer_roles()`` (a public
        accessor returning a snapshot copy) rather than reaching into
        ``graph._producer_roles``. Verifies the accessor exists and
        returns the expected set, so the seed's iteration is stable
        even if the graph internals change."""
        producers = implement_graph.producer_roles()
        # Returned set is a snapshot copy — mutating it must not affect
        # the graph.
        producers.add("not_a_real_role")
        assert "not_a_real_role" not in implement_graph.producer_roles()
        # Default implement-graph producers per get_default_implement_graph.
        assert implement_graph.producer_roles() == {"coder", "tester", "documenter"}


class TestTrackerDelegation:
    """The ``PeerConsensusTracker`` wrapper holds the lock and
    delegates to the matrix. These tests are thin — the heavy lifting
    is covered above."""

    def test_tracker_delegates_to_matrix(self, implement_graph):
        tracker = PeerConsensusTracker(
            pipeline_id="test-pipeline",
            graph=implement_graph,
        )
        auto_acked = tracker.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert auto_acked == ["coder"]
        assert tracker.matrix.is_fully_acked("coder") is True

    def test_tracker_noop_when_all_producers_present(self, implement_graph):
        tracker = PeerConsensusTracker(
            pipeline_id="test-pipeline",
            graph=implement_graph,
        )
        auto_acked = tracker.seed_auto_ack_for_empty_pure_producers(
            {"coder", "tester", "documenter"}
        )
        assert auto_acked == []


def _build_tracker(graph):
    """Build a real ``PeerConsensusTracker`` with every role in the
    graph registered.

    Uses RELAXED attestation strictness (the in-test default in the
    rest of ``orchestrator/tests/``) so test payloads don't need to
    populate every attestation field. The propose/ACK/confirm guards
    that gate consensus are unaffected — those run regardless of
    attestation strictness.
    """
    from peer_consensus import AttestationStrictness

    tracker = PeerConsensusTracker(
        pipeline_id="test-2581",
        graph=graph,
        cooldown_seconds=0,
        attestation_strictness=AttestationStrictness.RELAXED,
    )
    for role in graph.all_roles():
        tracker.register_agent(role)
    return tracker


def _propose_payload(summary="proposed work for #2581 end-to-end test"):
    return {
        "summary": summary,
        "artifacts": ["file.py"],
        "commit_sha": "deadbeef",
    }


def _ack_payload(version):
    return {
        "artifact_references": ["file.py"],
        "ack_version": version,
    }


class TestDocumenterOnlySliceEndToEnd:
    """End-to-end documenter-only slice flow through a real
    ``PeerConsensusTracker`` — the test the second review's blocking
    issue #1 asked for. CODER pre-seeded; DOCUMENTER + TESTER propose
    via ``handle_propose``; reviewers ACK via ``handle_ack``; the
    seeded CODER confirms via ``handle_confirmed``.

    This goes through:
      * ``check_propose_guard`` (the production gate that the seed
        bypasses for CODER but DOCUMENTER / TESTER pass through).
      * ``check_confirm_guard`` (the global-zero-proposal guard that
        previously deadlocked tester-only / documenter-only slices).
      * ``_collect_newly_ready_producers`` (the STATUS-nudge sweep
        the shortcut depends on — it's called from
        ``_handle_propose_inner`` and ``handle_ack``).
      * ``handle_confirmed`` for the seeded CODER (the production path
        the agent shortcut invokes via ``egg-orch consensus
        confirmed``).
    """

    def test_seeded_coder_confirms_via_handle_confirmed_after_peers_propose(self, implement_graph):
        """The full happy path: documenter-only slice, every active
        role in the graph reaches CONFIRMED through the production
        propose / ack / confirm handlers. Demonstrates the shortcut's
        runtime behavior end-to-end.

        Before #2581 the seeded CODER would have to call
        ``handle_propose`` and bump to v=2, invalidating the seeded
        v=1 ACKs and reopening the deadlock. After #2581 the agent
        skips propose and goes straight to ``handle_confirmed``;
        that's what this test exercises."""
        tracker = _build_tracker(implement_graph)
        # Pre-seed CODER (no coder tasks).
        seeded = tracker.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert seeded == ["coder"]
        # The seed put CODER at v=1 with every critical reviewer ACKed.
        assert tracker.matrix.is_fully_acked("coder") is True

        # DOCUMENTER does a normal propose via the production handler.
        doc_result = tracker.handle_propose("documenter", _propose_payload())
        assert doc_result["status"] == "proposed"
        assert doc_result["version"] == 1
        # No critical reviewers → fully-acked once proposed.
        assert tracker.matrix.is_fully_acked("documenter") is True

        # TESTER (dual-role) does its no-op propose. At the matrix level
        # this is a normal handle_propose — the no_test_changes_needed
        # attestation is validated separately and isn't relevant to the
        # consensus-reachability claim under test here.
        tester_result = tracker.handle_propose("tester", _propose_payload())
        assert tester_result["status"] == "proposed"
        tester_v = tester_result["version"]

        # Every critical reviewer of TESTER ACKs via handle_ack.
        for reviewer in implement_graph.critical_reviewers_for("tester"):
            ack_result = tracker.handle_ack(reviewer, "tester", _ack_payload(tester_v))
            assert ack_result["status"] == "acked"
        assert tracker.matrix.is_fully_acked("tester") is True

        # The seeded CODER now calls handle_confirmed — the exact path
        # the agent shortcut invokes. Before #2581 this would either
        # never be reached (CODER proposed first and broke the seed) or
        # be rejected with global_zero_proposal until every other
        # producer proposed.
        #
        # This is the core claim of the fix end-to-end: a seeded
        # empty-pure producer can confirm via the production
        # ``handle_confirmed`` path *without* going through propose,
        # once peers have proposed. handle_confirmed accepts CODER
        # because the seed made it fully-acked at v=1 AND every
        # producer in the graph has now proposed (so the global
        # zero-proposal guard clears).
        coder_confirm = tracker.handle_confirmed("coder")
        assert coder_confirm["status"] == "confirmed"
        # CODER is in the confirmed set after a successful confirm —
        # the matrix-level proof that the seed survived end-to-end.
        # Observed through the public ``confirmed_roles`` property
        # rather than reaching into ``tracker._confirmed`` directly.
        assert "coder" in tracker.confirmed_roles

    def test_seeded_coder_confirm_rejected_before_peers_propose(self, implement_graph):
        """The shortcut's expected-pending-acks path: the seeded CODER
        calls ``handle_confirmed`` before any other producer has
        proposed. ``check_confirm_guard`` rejects with
        ``global_zero_proposal``, exactly what the shortcut text tells
        the agent to expect — the agent then blocks on the wait-loop
        until DOCUMENTER's propose triggers the STATUS nudge."""
        tracker = _build_tracker(implement_graph)
        tracker.seed_auto_ack_for_empty_pure_producers({"documenter"})

        # CODER tries to confirm without any peer having proposed.
        result = tracker.handle_confirmed("coder")
        # Production handler returns pending_acks with global_zero_proposal.
        assert result["status"] == "pending_acks"
        # The shortcut text references this exact field.
        assert "zero_proposal_producers" in result
        # DOCUMENTER and TESTER are the two producers that haven't
        # proposed yet; they're surfaced in the response so the agent
        # could log them.
        assert set(result["zero_proposal_producers"]) >= {"documenter", "tester"}

    def test_seeded_coder_wakes_via_status_nudge_after_peer_proposes(self, implement_graph):
        """The shortcut's STATUS-wakeup path. After CODER's first
        ``handle_confirmed`` call is rejected with
        ``global_zero_proposal``, DOCUMENTER's propose (or any other
        peer's propose) calls ``_collect_newly_ready_producers`` —
        which is what would emit the directed ``STATUS
        ready_to_confirm`` nudge that the agent's wait-loop is blocked
        on. This test pins that the seeded CODER appears in the
        ``newly_ready`` list returned by ``handle_propose`` once the
        last peer's propose makes the global-zero-proposal guard clear.

        ``newly_ready`` is the payload the orchestrator uses to drive
        ``_emit_ready_to_confirm_nudges`` (the STATUS message that
        wakes the agent). If this regresses, the shortcut's wait-loop
        hangs.
        """
        tracker = _build_tracker(implement_graph)
        tracker.seed_auto_ack_for_empty_pure_producers({"documenter"})

        # First peer proposes — DOCUMENTER. Global zero-proposal guard
        # still blocks (TESTER hasn't proposed yet), so CODER isn't yet
        # ready_to_confirm.
        doc_result = tracker.handle_propose("documenter", _propose_payload())
        # newly_ready is the source of the directed STATUS nudge.
        doc_ready_roles = {item["role"] for item in doc_result["newly_ready"]}
        # CODER not yet ready: TESTER hasn't proposed.
        assert "coder" not in doc_ready_roles

        # Second peer (TESTER) proposes. Now every producer has
        # proposed; CODER's full-ACKed-at-v=1 state means
        # check_confirm_guard finally accepts CODER and the sweep
        # surfaces it. _emit_ready_to_confirm_nudges in production
        # turns this into the STATUS message the agent's wait-loop
        # blocks on.
        tester_result = tracker.handle_propose("tester", _propose_payload())
        tester_ready_roles = {item["role"] for item in tester_result["newly_ready"]}
        # CODER is now in the newly_ready list — the STATUS nudge fires.
        assert "coder" in tester_ready_roles, (
            f"CODER should be ready_to_confirm after TESTER proposes; "
            f"newly_ready was {tester_result['newly_ready']}"
        )

        # And calling handle_confirmed for CODER now succeeds.
        confirm_result = tracker.handle_confirmed("coder")
        assert confirm_result["status"] in ("confirmed", "partially_confirmed")

    def test_dual_role_tester_nack_breaks_seeded_acks_and_rejects_confirm(self, implement_graph):
        """The dual-role recovery scenario the second review's
        shortcut docstring describes. After the seed, TESTER's
        producer-side work uncovers a need for code that wasn't in the
        slice plan. TESTER NACKs CODER at v=1 — the matrix records the
        NACK at the seeded version, ``is_fully_acked("coder")`` drops
        to False, and a subsequent CODER ``handle_confirmed`` is
        rejected with ``producer_not_fully_acked``. The shortcut text
        instructs the agent to call
        ``mcp__sdlc__register_open_question`` at this point rather than
        silently start producing."""
        tracker = _build_tracker(implement_graph)
        tracker.seed_auto_ack_for_empty_pure_producers({"documenter"})
        assert tracker.matrix.is_fully_acked("coder") is True

        # DOCUMENTER + TESTER propose so the global zero-proposal guard
        # is otherwise satisfied.
        tracker.handle_propose("documenter", _propose_payload())
        tracker.handle_propose("tester", _propose_payload())

        # TESTER (dual-role) NACKs CODER at the seeded version.
        nack_payload = {
            "ack_version": 1,
            "nack_version": 1,
            "artifact_references": ["file.py"],
            "reason": (
                "no_test_changes_needed=false would be required — slice plan missing coder task"
            ),
        }
        tracker.handle_nack("tester", "coder", nack_payload)
        assert tracker.matrix.is_fully_acked("coder") is False

        # CODER's confirm is now rejected — the shortcut's "if it
        # returns pending_acks with producer_not_fully_acked" branch.
        # ``handle_confirmed`` returns ``message=guard.reason`` (see
        # ``peer_consensus.py``), and the producer-not-fully-acked
        # reason string is
        # ``f"Producer {agent_role} cannot confirm: not fully ACKed. ..."``
        # (``action_guards.py``). The ``producer_not_fully_acked``
        # guard-name literal lives in ``guard.details`` only, not the
        # message — assert on the reason substring.
        result = tracker.handle_confirmed("coder")
        assert result["status"] == "pending_acks"
        assert "not fully ACKed" in result["message"]


class TestDeriveProducerRolesWithTasks:
    """Exercises ``routes.pipelines._derive_producer_roles_with_tasks``
    end-to-end via the module's ``load_contract`` import (#2581).

    The previous test pass-through asserted only that an untouched
    matrix had ``proposal_version == 0`` — the review (issue #1, second
    pass) flagged that those tests never exercised the catch logic or
    the slice-id-not-found branch. These tests patch the contract
    loader directly so the production helper executes its real branches.
    """

    def _patch_loader(self, monkeypatch, loader_fn):
        """Patch the module-level ``load_contract`` import seen by
        ``_derive_producer_roles_with_tasks`` so we exercise its real
        try/except and slice-lookup logic without spinning up a
        contract on disk.

        ``_derive_producer_roles_with_tasks`` performs an
        ``from egg_contracts.loader import load_contract`` *inside*
        the function. Patching the symbol on ``egg_contracts.loader``
        is the right seam — the import sees the patched symbol when
        the helper runs.
        """
        import egg_contracts.loader as loader_module

        monkeypatch.setattr(loader_module, "load_contract", loader_fn)

    def test_returns_none_when_slice_id_is_none(self):
        """CUSTOM-mode / prompt-mode pipelines have no slice id — the
        helper short-circuits with ``None`` and the loader is never
        called. This is the pre-#2581 no-seed path."""
        from routes.pipelines import _derive_producer_roles_with_tasks

        # No loader patch — if the helper called load_contract here, the
        # real implementation would fail because /tmp isn't a real repo.
        result = _derive_producer_roles_with_tasks(
            pipeline_id="pid",
            slice_id=None,
            has_contract=True,
            worktree_repo_path=Path("/tmp/nonexistent"),
        )
        assert result is None

    def test_returns_none_when_pipeline_has_no_contract(self):
        """``has_contract=False`` (BABYSIT / CUSTOM-mode with no
        contract draft) short-circuits the same way — the helper never
        attempts to load."""
        from routes.pipelines import _derive_producer_roles_with_tasks

        result = _derive_producer_roles_with_tasks(
            pipeline_id="pid",
            slice_id="slice-1",
            has_contract=False,
            worktree_repo_path=Path("/tmp/nonexistent"),
        )
        assert result is None

    def test_returns_role_set_from_loaded_contract(self, monkeypatch):
        """Happy path: loader returns a contract whose slice has tasks
        across all three producer roles. The helper returns the set of
        roles (with ``Task.role=None`` mapped to ``coder`` per the
        contract schema's execution-time default)."""
        from routes.pipelines import _derive_producer_roles_with_tasks

        class _Task:
            def __init__(self, role):
                self.role = role

        class _Slice:
            def __init__(self, sid, tasks):
                self.id = sid
                self.tasks = tasks

        class _Contract:
            def __init__(self, slices):
                self.slices = slices

        # role=None → coder; explicit roles preserved.
        slice_obj = _Slice(
            "slice-1",
            [_Task(role=None), _Task(role="tester"), _Task(role="documenter")],
        )
        contract = _Contract([slice_obj])

        captured: dict = {}

        def _fake_loader(pid, path):
            captured["pid"] = pid
            captured["path"] = path
            return contract

        self._patch_loader(monkeypatch, _fake_loader)
        result = _derive_producer_roles_with_tasks(
            pipeline_id="pid-42",
            slice_id="slice-1",
            has_contract=True,
            worktree_repo_path=Path("/tmp/wt"),
        )
        assert result == {"coder", "tester", "documenter"}
        # The loader was called with the pipeline id and worktree path.
        assert captured["pid"] == "pid-42"
        assert captured["path"] == Path("/tmp/wt")

    def test_documenter_only_slice_returns_documenter_only(self, monkeypatch):
        """A documenter-only slice yields ``{"documenter"}`` —
        downstream ``empty_pure_producers`` will pick up CODER as a
        pure-producer empty-of-tasks role and the seed will fire."""
        from routes.pipelines import _derive_producer_roles_with_tasks

        class _Task:
            def __init__(self, role):
                self.role = role

        class _Slice:
            def __init__(self, sid, tasks):
                self.id = sid
                self.tasks = tasks

        class _Contract:
            def __init__(self, slices):
                self.slices = slices

        contract = _Contract([_Slice("slice-1", [_Task(role="documenter")])])
        self._patch_loader(monkeypatch, lambda pid, path: contract)

        result = _derive_producer_roles_with_tasks(
            pipeline_id="pid",
            slice_id="slice-1",
            has_contract=True,
            worktree_repo_path=Path("/tmp/wt"),
        )
        assert result == {"documenter"}

    @pytest.mark.parametrize(
        "exc_factory",
        [
            # The three narrow exception types the helper catches.
            # ContractNotFoundError / ContractValidationError have
            # their own __init__ signatures — construct them the way
            # the real loader does.
            lambda: __import__(
                "egg_contracts.loader", fromlist=["ContractNotFoundError"]
            ).ContractNotFoundError("pid-test", Path("/tmp/wt/.egg-state/contracts")),
            lambda: __import__(
                "egg_contracts.loader", fromlist=["ContractValidationError"]
            ).ContractValidationError("pid-test", ["bad field x"]),
            lambda: OSError("io broken"),
        ],
        ids=["ContractNotFoundError", "ContractValidationError", "OSError"],
    )
    def test_returns_none_on_narrow_loader_exception(self, monkeypatch, exc_factory):
        """Each of the three narrow exception types the helper
        catches: the helper returns ``None`` and emits a structured
        WARNING with the error_type / pipeline_id inlined. This is the
        safety-net-off condition operators must see in default log
        output — DEBUG-level fallbacks would hide it.

        Patches ``routes.pipelines.logger`` directly (the same pattern
        ``test_slice_1_context_branch_base_resolution.py`` uses) since
        the project's structlog logger writes through a module-level
        ``logger`` object that intercept-tests are expected to mock.
        """
        from unittest.mock import MagicMock, patch

        from routes.pipelines import _derive_producer_roles_with_tasks

        def _raising_loader(pid, path):
            raise exc_factory()

        self._patch_loader(monkeypatch, _raising_loader)

        with patch("routes.pipelines.logger") as mock_logger:
            mock_logger.warning = MagicMock()
            result = _derive_producer_roles_with_tasks(
                pipeline_id="pid-42",
                slice_id="slice-1",
                has_contract=True,
                worktree_repo_path=Path("/tmp/wt"),
            )
        assert result is None
        # Exactly one WARNING about the failed load.
        assert mock_logger.warning.called, (
            "expected logger.warning called for the narrow-exception catch path"
        )
        # The message contains the stable "Could not derive ..." prefix
        # and the structured fields carry the error_type + pipeline_id
        # so operators can grep for the safety-net-off signal.
        msg_args = [call.args[0] for call in mock_logger.warning.call_args_list if call.args]
        assert any("Could not derive producer_roles_with_tasks" in m for m in msg_args), (
            f"expected stable WARNING message, got: {msg_args}"
        )
        # Inspect kwargs of the first matching call — error_type and
        # pipeline_id are required for operators to diagnose the skew.
        for call in mock_logger.warning.call_args_list:
            if call.args and "Could not derive" in call.args[0]:
                assert call.kwargs.get("pipeline_id") == "pid-42"
                assert call.kwargs.get("slice_id") == "slice-1"
                assert call.kwargs.get("error_type") is not None
                break
        else:
            pytest.fail("expected matching 'Could not derive' warning call")

    def test_unknown_exception_propagates(self, monkeypatch):
        """The catch is narrow on purpose — schema bumps or
        ``AttributeError`` on contract model changes must propagate so
        they fail loudly during testing rather than silently
        re-introducing the deadlock in production. This is the exact
        bare-``except Exception`` antipattern the previous review
        called out; this test pins down that it cannot regress."""
        from routes.pipelines import _derive_producer_roles_with_tasks

        def _raising_loader(pid, path):
            raise AttributeError("contract schema changed shape")

        self._patch_loader(monkeypatch, _raising_loader)
        with pytest.raises(AttributeError, match="contract schema changed"):
            _derive_producer_roles_with_tasks(
                pipeline_id="pid",
                slice_id="slice-1",
                has_contract=True,
                worktree_repo_path=Path("/tmp/wt"),
            )

    def test_returns_none_when_slice_id_not_in_contract(self, monkeypatch):
        """Slice id well-formed but absent from the loaded contract —
        likely a contract-on-main vs slice-on-branch skew or a stale
        slice id passed in. The helper logs a WARNING with the
        contract's available slice ids inlined (so operators can
        diagnose the skew from the log line) and returns ``None``.

        Patches ``routes.pipelines.logger`` directly — same pattern as
        the narrow-exception test above.
        """
        from unittest.mock import MagicMock, patch

        from routes.pipelines import _derive_producer_roles_with_tasks

        class _Slice:
            def __init__(self, sid):
                self.id = sid
                self.tasks: list = []

        class _Contract:
            def __init__(self, slices):
                self.slices = slices

        contract = _Contract([_Slice("slice-1"), _Slice("slice-2")])
        self._patch_loader(monkeypatch, lambda pid, path: contract)

        with patch("routes.pipelines.logger") as mock_logger:
            mock_logger.warning = MagicMock()
            result = _derive_producer_roles_with_tasks(
                pipeline_id="pid-99",
                slice_id="slice-99",  # not in the contract
                has_contract=True,
                worktree_repo_path=Path("/tmp/wt"),
            )
        assert result is None
        # The stable WARNING line includes the contract's available
        # slice ids in the structured payload — operators can spot
        # contract-vs-branch skew without digging through DEBUG logs.
        for call in mock_logger.warning.call_args_list:
            if call.args and "Slice id not found in contract" in call.args[0]:
                assert call.kwargs.get("pipeline_id") == "pid-99"
                assert call.kwargs.get("slice_id") == "slice-99"
                # available_slice_ids carries the literal slice ids the
                # contract knows about — required for the skew
                # diagnosis log path.
                assert call.kwargs.get("available_slice_ids") == ["slice-1", "slice-2"]
                break
        else:
            pytest.fail(
                "expected 'Slice id not found in contract' WARNING; "
                f"got: {mock_logger.warning.call_args_list}"
            )


class TestEmptyPureProducersPredicate:
    """Pins down ``ReviewGraph.empty_pure_producers`` (#2581) — the
    single source of truth for the empty-pure-producer predicate used
    by both the matrix seed and the prompt-level shortcut flag.

    The previous review (non-blocking #4, second pass) flagged that
    the predicate was duplicated in two places: the matrix seeder's
    inline loop and ``_run_concurrent_phase``'s
    ``_pre_seeded_empty_producer_roles`` computation. If one drifts
    (e.g. a future change adds a third skip condition), the prompt
    flag and the matrix seed go out of sync. These tests pin the
    helper down so both call sites can rely on identical semantics.
    """

    def test_predicate_skips_dual_role_producers(self, implement_graph):
        """TESTER (dual-role) must NEVER appear in
        ``empty_pure_producers`` regardless of whether it has tasks —
        the seed depends on TESTER always running so it can ACK or
        NACK CODER."""
        # No tasks for any producer — TESTER would otherwise look
        # eligible but is excluded by the dual-role check.
        result = implement_graph.empty_pure_producers(producers_with_tasks=set())
        assert "tester" not in result
        # Pure producers with no tasks are eligible.
        assert "coder" in result
        assert "documenter" in result

    def test_predicate_skips_producers_with_tasks(self, implement_graph):
        """A producer that has at least one task in the slice is not
        empty and must not be auto-ACKed."""
        result = implement_graph.empty_pure_producers(producers_with_tasks={"coder", "documenter"})
        # Both pure producers have tasks → neither is empty-pure.
        assert result == set()

    def test_matrix_seed_and_prompt_flag_agree(self, implement_graph):
        """End-to-end of non-blocking #4: the matrix seeder and the
        prompt-level shortcut flag MUST yield the same set of roles,
        because they both route through ``empty_pure_producers``. If
        one drifts the seed and prompt go out of sync and the agent
        either skips propose without a seeded matrix entry (deadlock)
        or proposes against a seeded matrix entry (version bump
        deadlock — exactly what #2581 fixed)."""
        matrix = ApprovalMatrix(implement_graph)
        producers_with_tasks = {"documenter"}

        # Matrix seed says these roles got auto-ACKed:
        auto_acked = matrix.seed_auto_ack_for_empty_pure_producers(producers_with_tasks)

        # The prompt-flag computation (mirrored from _run_concurrent_phase)
        # uses the same helper:
        prompt_flag_roles = implement_graph.empty_pure_producers(producers_with_tasks)

        # The two are the same set. If a future change breaks this
        # invariant, the seed and prompt will silently disagree.
        assert set(auto_acked) == prompt_flag_roles


class TestProducerOrientationPreSeededShortcut:
    """Tests that the BRC preamble injects the empty-producer shortcut
    block when ``is_pre_seeded_empty_producer=True`` (#2581).

    The shortcut tells CODER / DOCUMENTER to skip the propose step
    entirely — without this, the agent's real propose at v=2 would
    invalidate the seeded v=1 ACKs and re-trigger the deadlock. The
    block is the end-to-end wire-up between the matrix-level seed and
    the agent's runtime behaviour.
    """

    def test_shortcut_block_appears_for_pre_seeded_coder(self):
        """A pre-seeded coder's BRC preamble must include the shortcut
        block telling it to skip propose and confirm directly."""
        from routes.pipelines import _build_brc_preamble

        preamble = _build_brc_preamble(
            role_value="coder",
            phase="implement",
            repo="jwbron/egg",
            branch="main",
            is_pre_seeded_empty_producer=True,
        )
        assert "Pre-seeded empty-producer shortcut" in preamble
        assert "Do NOT run `egg-orch consensus propose`" in preamble
        # The shortcut should explicitly route through CONFIRM.
        assert "consensus confirmed" in preamble

    def test_shortcut_block_absent_when_flag_false(self):
        """The shortcut block must NOT appear when the agent has tasks
        (the normal flow) — otherwise CODER with real tasks would skip
        propose and break the slice."""
        from routes.pipelines import _build_brc_preamble

        preamble = _build_brc_preamble(
            role_value="coder",
            phase="implement",
            repo="jwbron/egg",
            branch="main",
            is_pre_seeded_empty_producer=False,
        )
        assert "Pre-seeded empty-producer shortcut" not in preamble

    def test_shortcut_block_appears_for_pre_seeded_documenter(self):
        """Documenter pure-producer path is symmetric with coder."""
        from routes.pipelines import _build_brc_preamble

        preamble = _build_brc_preamble(
            role_value="documenter",
            phase="implement",
            repo="jwbron/egg",
            branch="main",
            is_pre_seeded_empty_producer=True,
        )
        assert "Pre-seeded empty-producer shortcut" in preamble
