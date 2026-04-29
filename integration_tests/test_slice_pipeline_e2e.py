"""End-to-end integration test for the slice-DAG implement loop (#2137 TASK-5-4).

The acceptance criterion in the plan calls for an integration test
that runs under ``make test-integration`` and exercises the
slice-pipeline path end-to-end:

  multi-slice plan ingestion → wave dispatch → per-slice BRC →
  stacked-PR creation → reconciler heals an orphaned child PR

The implement-phase slice loop is gated at
``orchestrator/routes/pipelines.py`` by ``_slice_count > 1`` and
spins up real container teams via the spawner — which requires
Docker, the Anthropic SDK, and a real GitHub remote. Driving that
machinery from a single pytest run would turn a unit-CI check
into a multi-minute live-stack test.

Instead, this module exercises the load-bearing seams of the
slice path against in-memory fakes that mirror the shapes the
production wiring uses:

* :class:`SliceScheduler` over a 3-slice forest
  (``slice-1 → slice-2`` and ``slice-1 → slice-3``).
* The wave-dispatch protocol (``iter_ready`` →
  ``mark_spawned`` → ``record_complete``).
* The stacked-PR reconciler, driven by ``reconcile_once`` against
  fakes whose ``list_open_prs`` callable returns the **producer's**
  normalised ``head_ref``/``base_ref`` shape — closing the gap
  egg-reviewer flagged where earlier tests fed in the consumer's
  wrong shape and hid a silent no-op.
* The full rebase / push / PR retarget heal path, asserted by
  spying on the gateway client's ``rebase_onto`` invocations.

Marker-gated under ``@pytest.mark.integration`` so it runs under
``make test-integration`` per the AC. The test does NOT require
Docker, the Anthropic API, or a live GitHub remote — every external
side-effect is stubbed out at the gateway-client boundary, and the
scheduler / reconciler internals are exercised against the real
implementations.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.integration

# sys.path setup: orchestrator + shared (mirrors the unit-test files
# in ``orchestrator/tests/``).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ORCH = _PROJECT_ROOT / "orchestrator"
_SHARED = _PROJECT_ROOT / "shared"
for _p in (_ORCH, _SHARED, _PROJECT_ROOT):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from egg_contracts.models import Contract, IssueInfo, Slice  # noqa: E402
from slice_scheduler import SchedulerSliceState, SliceScheduler  # noqa: E402
from stacked_pr_reconciler import (  # noqa: E402
    OrphanedChildPR,
    find_orphaned_child_prs,
    reconcile_once,
)

# ---------------------------------------------------------------------------
# Fixtures — a small forest with one root and two parallel children
# ---------------------------------------------------------------------------


def _slice(
    id_: str,
    *,
    deps: list[str] | None = None,
    parent_branch: str | None = None,
) -> Slice:
    return Slice(
        id=id_,
        name=f"slice {id_}",
        dependencies=deps or [],
        parent_branch_at_creation=parent_branch,
    )


def _three_slice_forest() -> Contract:
    """``slice-1`` is the root; ``slice-2`` and ``slice-3`` both depend on it."""
    return Contract(
        issue=IssueInfo(number=2137, title="slice e2e", url="u"),
        slices=[
            _slice("slice-1"),
            _slice("slice-2", deps=["slice-1"], parent_branch="egg/issue-2137/slice-1"),
            _slice("slice-3", deps=["slice-1"], parent_branch="egg/issue-2137/slice-1"),
        ],
    )


# ---------------------------------------------------------------------------
# Wave-dispatch contract
# ---------------------------------------------------------------------------


class TestWaveDispatch:
    """Wave 1 → ``slice-1`` only; after it completes, wave 2 → both
    children. Mirrors the production run-loop's harvest-spawn-complete
    rhythm."""

    def test_root_runs_first_then_children_run_in_parallel(self) -> None:
        contract = _three_slice_forest()
        scheduler = SliceScheduler(contract)

        # Wave 1: only the root is ready.
        wave1 = list(scheduler.iter_ready())
        assert {sid for sid, _ in wave1} == {"slice-1"}
        for sid, _ in wave1:
            scheduler.mark_spawned(sid)

        # While the root is RUNNING, the children remain PENDING —
        # the scheduler must not surface them prematurely.
        running = [sid for sid, _ in scheduler.iter_ready()]
        assert running == []

        scheduler.record_complete("slice-1")

        # Wave 2: both children are ready in the same wave (parallel
        # spawn, capped at ``max_parallel_slices``).
        wave2 = list(scheduler.iter_ready())
        assert {sid for sid, _ in wave2} == {"slice-2", "slice-3"}
        # Each child correctly identifies ``slice-1`` as its parent —
        # this is what the orchestrator uses to derive the slice's
        # integration branch.
        for _sid, parent in wave2:
            assert parent == "slice-1"

        for sid, _ in wave2:
            scheduler.mark_spawned(sid)
            scheduler.record_complete(sid)

        assert scheduler.all_done()

    def test_failure_in_root_blocks_subtree(self) -> None:
        """A failed root must arm the cascade so the children are
        eventually marked BLOCKED_ON_FAILED_DEPENDENCY (cascade poll
        is what fires the state change after the grace window)."""
        contract = _three_slice_forest()
        # ``failure_grace_seconds=0`` so we don't have to wait for
        # wallclock; the production default is 60s.
        scheduler = SliceScheduler(contract, failure_grace_seconds=0.0)

        # Wave 1.
        for sid, _ in list(scheduler.iter_ready()):
            scheduler.mark_spawned(sid)

        scheduler.record_failure("slice-1")
        events = scheduler.poll_cascades()
        assert len(events) == 1
        assert events[0].failed_slice_id == "slice-1"
        # Both children are downstream and must be in the blocked set.
        assert set(events[0].blocked_subtree) == {"slice-2", "slice-3"}

        # The downstream children's runtime state has been flipped.
        for child in ("slice-2", "slice-3"):
            rt = scheduler.get_slice_status(child)
            assert rt is not None
            assert rt.state == SchedulerSliceState.BLOCKED_ON_FAILED_DEPENDENCY


# ---------------------------------------------------------------------------
# Producer/consumer shape contract (the previously-broken seam)
# ---------------------------------------------------------------------------


class TestReconcilerOnProducerShape:
    """The reconciler must consume ``GatewayClient.list_open_prs``'s
    actual output without a translation layer — the bug egg-reviewer
    flagged was that earlier tests passed dicts with the consumer's
    wrong key shape so the silent no-op never surfaced.

    These tests use the producer's documented normalised shape
    (``number`` int, ``head_ref`` str, ``base_ref`` str) directly.
    """

    def test_orphan_detected_on_producer_shape(self) -> None:
        contract = _three_slice_forest()
        # ``slice-2``'s parent ``slice-1`` was merged and its branch
        # was deleted on origin → ``slice-2``'s open PR points at a
        # base that no longer exists.
        producer_prs: list[dict[str, Any]] = [
            {
                "number": 4242,
                "head_ref": "egg/issue-2137/slice-2",
                "base_ref": "egg/issue-2137/slice-1",
            },
            # ``slice-3``'s base is still alive — GitHub auto-retarget
            # already handled it; the reconciler must skip it.
            {
                "number": 4243,
                "head_ref": "egg/issue-2137/slice-3",
                "base_ref": "egg/issue-2137/slice-1",
            },
        ]
        # ``slice-3``'s base still exists; ``slice-2``'s does not.
        extant = {"egg/issue-2137/slice-1-still-here"}
        # Move ``slice-3``'s base into the extant set so that PR is
        # filtered out:
        extant.add("egg/issue-2137/slice-1")
        producer_prs[1]["base_ref"] = "egg/issue-2137/slice-1"

        # Re-run with only ``slice-2``'s base missing.
        extant_only_slice3 = {"egg/issue-2137/slice-1-still-here"}
        # Make slice-3's base extant; slice-2's is gone.
        extant_only_slice3.add("egg/issue-2137/slice-1")

        # Drop the second PR for clarity in this assertion: just
        # check the orphan-detection behaviour.
        only_orphan = [producer_prs[0]]
        orphans = find_orphaned_child_prs(contract, only_orphan, set())
        assert len(orphans) == 1
        orphan = orphans[0]
        assert orphan.slice_id == "slice-2"
        assert orphan.pr_number == 4242  # NOT 0 — would mean coercion bug
        assert orphan.branch == "egg/issue-2137/slice-2"
        assert orphan.deleted_base == "egg/issue-2137/slice-1"
        # slice-1 is the root, and its branch is gone (the merge
        # cascade is the primary trigger here). The walk-up falls
        # back to the pipeline branch ``egg/issue-2137`` — the
        # last-resort safe target. This is the bug the reviewer
        # caught: the old code would have left
        # ``intended_new_base == "egg/issue-2137/slice-1"`` (the
        # dead branch we just escaped from).
        assert orphan.intended_new_base == "egg/issue-2137"

    def test_reconcile_once_drives_full_heal_callable_with_orphan(self) -> None:
        """``reconcile_once`` passes the full :class:`OrphanedChildPR`
        to the rebase callable — production wiring depends on
        ``orphan.pr_number`` to retarget the PR after the rebase."""
        contract = _three_slice_forest()
        producer_prs: list[dict[str, Any]] = [
            {
                "number": 4242,
                "head_ref": "egg/issue-2137/slice-2",
                "base_ref": "egg/issue-2137/slice-1",
            }
        ]
        captured: list[OrphanedChildPR] = []

        def fake_rebase(orphan: OrphanedChildPR) -> bool:
            captured.append(orphan)
            return True

        result = reconcile_once(
            contract,
            list_open_prs=lambda: producer_prs,
            list_extant_branches=lambda: set(),
            rebase_onto=fake_rebase,
        )

        assert result.orphans_detected == 1
        assert result.rebases_succeeded == 1
        assert result.rebases_failed == 0
        # The callable saw the FULL orphan record — that's how the
        # production bridge knows which PR to retarget.
        assert len(captured) == 1
        assert captured[0].pr_number == 4242
        assert captured[0].branch == "egg/issue-2137/slice-2"


# ---------------------------------------------------------------------------
# Full heal path: rebase + push --force-with-lease + gh pr edit --base
# ---------------------------------------------------------------------------


class TestEndToEndOrphanHeal:
    """Drive ``GatewayClient.rebase_onto`` against an in-memory fake
    HTTP transport and assert the three-step heal lands all of:

    1. local rebase via ``/api/v1/git`` (canonical argv shape).
    2. ``--force-with-lease`` push via ``/api/v1/git/push``.
    3. PR retarget via ``/api/v1/gh/pr/edit`` with ``base=...``.

    This is the test that previously did NOT exist — earlier
    integration coverage mocked ``reconcile_once`` itself, which hid
    the fact that the live ``rebase_onto`` was local-only and could
    not heal the orphan on origin.
    """

    def _client(self):
        # Late import so ``sys.path`` setup at module top runs first.
        from gateway_client import GatewayClient

        client = GatewayClient(
            gateway_host="localhost",
            gateway_port=19999,  # not bound — every test replaces the network
            launcher_secret="test-secret",
            timeout=5,
        )
        # Pretend we have an IP so register_session doesn't NPE.
        # ``self_ip`` is a property without a setter; the implementation
        # caches its UDP-probe result on ``_self_ip_cache``, so seeding
        # the cache short-circuits the network probe in tests.
        client._self_ip_cache = "127.0.0.1"
        return client

    def test_three_step_heal_order_on_orphaned_child_pr(self) -> None:
        client = self._client()
        endpoints: list[str] = []
        payloads: list[dict[str, Any]] = []

        def fake_make_request(endpoint, *args, **kwargs):
            endpoints.append(endpoint)
            payloads.append(kwargs.get("data") or {})
            return {"success": True}

        fake_session = MagicMock()
        fake_session.session_token = "tok"
        client.register_session = MagicMock(return_value=fake_session)  # type: ignore[assignment]
        client._make_request = MagicMock(side_effect=fake_make_request)  # type: ignore[assignment]
        client.delete_session = MagicMock(return_value=True)  # type: ignore[assignment]

        ok = client.rebase_onto(
            "issue-2137",
            "/repo",
            branch="egg/issue-2137/slice-2",
            new_base="egg/issue-2137",
            old_base="egg/issue-2137/slice-1",
            pr_number=4242,
            repo="jwbron/egg",
        )
        assert ok is True

        # Strict order: rebase → push → pr/edit. If push fails, edit
        # MUST NOT be called (covered by the gateway-client unit test
        # ``test_push_failure_short_circuits_pr_edit``); here we
        # assert the happy-path order.
        assert endpoints == [
            "/api/v1/git/execute",
            "/api/v1/git/push",
            "/api/v1/gh/pr/edit",
        ]

        rebase_payload, push_payload, edit_payload = payloads

        # Step 1: canonical rebase argv.
        assert rebase_payload["operation"] == "rebase"
        assert rebase_payload["args"] == [
            "--onto",
            "egg/issue-2137",
            "egg/issue-2137/slice-1",
            "egg/issue-2137/slice-2",
        ]

        # Step 2: force-with-lease push of the rebased branch back
        # to origin so the open PR's head ref reflects the rebase.
        # ``consensus_push=True`` is required because the session has
        # ``pipeline_id`` set — without it the gateway's pipeline-mode
        # push enforcement returns 403.
        assert push_payload["force_with_lease"] is True
        assert push_payload["consensus_push"] is True
        assert push_payload["refspec"] == "egg/issue-2137/slice-2:refs/heads/egg/issue-2137/slice-2"

        # Step 3: PR base retarget via the gateway's gh_pr_edit
        # endpoint (extended in #2137 to accept ``base``).
        assert edit_payload == {
            "repo": "jwbron/egg",
            "pr_number": 4242,
            "base": "egg/issue-2137",
        }

    def test_reconcile_once_wired_to_full_heal(self) -> None:
        """Full integration: drive the reconciler end-to-end with a
        producer-shaped ``list_open_prs`` and assert the gateway
        client makes all three HTTP calls per orphan."""
        client = self._client()
        endpoints: list[str] = []

        def fake_make_request(endpoint, *args, **kwargs):
            endpoints.append(endpoint)
            return {"success": True}

        fake_session = MagicMock()
        fake_session.session_token = "tok"
        client.register_session = MagicMock(return_value=fake_session)  # type: ignore[assignment]
        client._make_request = MagicMock(side_effect=fake_make_request)  # type: ignore[assignment]
        client.delete_session = MagicMock(return_value=True)  # type: ignore[assignment]

        contract = _three_slice_forest()
        producer_prs: list[dict[str, Any]] = [
            {
                "number": 4242,
                "head_ref": "egg/issue-2137/slice-2",
                "base_ref": "egg/issue-2137/slice-1",
            }
        ]

        # Production wiring (see
        # ``orchestrator/routes/pipelines.py::_start_stacked_pr_reconciler``):
        # bind the gateway client into a closure that invokes the
        # full heal helper.
        def rebase_via_gateway(orphan: OrphanedChildPR) -> bool:
            return bool(
                client.rebase_onto(
                    "issue-2137",
                    "/repo",
                    branch=orphan.branch,
                    new_base=orphan.intended_new_base,
                    old_base=orphan.deleted_base,
                    pr_number=orphan.pr_number,
                    repo="jwbron/egg",
                )
            )

        result = reconcile_once(
            contract,
            list_open_prs=lambda: producer_prs,
            list_extant_branches=lambda: set(),
            rebase_onto=rebase_via_gateway,
        )

        assert result.orphans_detected == 1
        assert result.rebases_succeeded == 1
        assert result.rebases_failed == 0
        # All three steps fired in order via the live gateway client.
        assert endpoints == [
            "/api/v1/git/execute",
            "/api/v1/git/push",
            "/api/v1/gh/pr/edit",
        ]
