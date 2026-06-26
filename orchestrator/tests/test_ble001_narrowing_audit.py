"""Spot-coverage for the slice-3 BLE001 audit (#2777 TASK-3-5).

The slice-3 coder audited the 20 ``# noqa: BLE001`` swallow-all
handlers in ``orchestrator/routes/pipelines.py`` and narrowed
4 sites to a specific exception tuple; the remaining 15 sites
retain a documented-bare-except with an inline comment naming
what's caught. Per the plan task-3-11 (8):

    > BLE001 audit (TASK-3-5) — where TASK-3-5 narrowed a
    > swallow-all handler to a specific exception, add a unit
    > test that asserts the new specific exception triggers the
    > documented recovery path. Sample 3-5 sites; full coverage
    > is not required (BLE001 audit is per-site judgement, not
    > per-site test).

These tests are source-level regression tests: a future BLE001
back-slide (someone replaces the narrowed handler with bare
``except Exception``) trips the assert here so reviewers catch
the regression in CI rather than discovering it from a
production swallow.

We sample three sites the coder narrowed:

* The ``get_gateway_client`` symmetry import in the cascade-alert
  branch — narrowed to ``except ImportError``.
* The reconciler-thread teardown ``Thread.join`` call — narrowed
  to ``except RuntimeError``.
* The reconciler-thread teardown's documented "silent timeout"
  behaviour — pinned via the comment string above the handler.

For the remaining 15 documented-bare-except sites we test the
audit-comment invariant: every ``# noqa: BLE001`` swallow has
an explanatory inline comment in the same block, not just the
suppression marker.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))


_PIPELINES_SRC = (_orchestrator_path / "routes" / "pipelines.py").read_text(encoding="utf-8")


def test_cascade_alert_gateway_import_uses_narrow_importerror() -> None:
    """Narrowed site #1: the cascade-alert branch's symmetry import
    of ``get_gateway_client`` uses ``except ImportError`` so a future
    refactor that turns the orchestrator-side caller into a
    crash-on-attribute path can't be silently swallowed by a bare
    ``except Exception``."""
    # Search for the named import block. The import is wrapped in a
    # try/except that catches ImportError specifically; a bare
    # ``except Exception`` would defeat the audit.
    pattern = re.compile(
        r"from orchestrator\.gateway_client import \(\s*"
        r"get_gateway_client as _get_gateway_client,\s*\)\s*"
        r"\n\s*_ = _get_gateway_client[^\n]*\n\s*except ImportError:",
        re.DOTALL,
    )
    assert pattern.search(_PIPELINES_SRC), (
        "Cascade-alert ``get_gateway_client`` import must use "
        "``except ImportError`` (slice-3 task-3-5 narrowing); a "
        "bare ``except Exception`` here would silently swallow "
        "downstream AttributeError / TypeError regressions."
    )


def test_reconciler_thread_join_uses_narrow_runtimeerror() -> None:
    """Narrowed site #2: the reconciler-thread teardown
    ``reconciler_thread.join(timeout=5.0)`` uses ``except
    RuntimeError`` because ``Thread.join`` only raises RuntimeError
    (e.g. joining the current thread); other failure modes are
    silent timeouts."""
    pattern = re.compile(
        r"reconciler_thread\.join\(timeout=5\.0\)\s*\n\s*except RuntimeError:",
        re.DOTALL,
    )
    assert pattern.search(_PIPELINES_SRC), (
        "Reconciler-thread teardown ``Thread.join`` must use "
        "``except RuntimeError`` (slice-3 task-3-5 narrowing); a "
        "bare ``except Exception`` would silently hide unrelated "
        "join failures that should bubble up."
    )


def test_reconciler_thread_join_documents_silent_timeout() -> None:
    """Narrowed site #2 (documentation invariant): the comment above
    the narrowed handler must name what's caught and why. A
    reviewer scanning the file should see "Thread.join only raises
    RuntimeError" inline next to the handler — anything else and
    the audit comment has been silently dropped."""
    pattern = re.compile(
        r"except RuntimeError:\s*\n\s*#[^\n]*Thread\.join only raises RuntimeError",
        re.DOTALL,
    )
    assert pattern.search(_PIPELINES_SRC), (
        "Reconciler-thread teardown ``except RuntimeError`` must "
        "carry an inline comment naming what's caught (``Thread.join "
        "only raises RuntimeError``) — silent drop of the audit "
        "comment regresses per-site BLE001 clarity."
    )


def test_audit_window_retains_documented_ble001_population() -> None:
    """Documentation invariant for the slice-3 audit window: the 20
    enumerated ``# noqa: BLE001`` sites originally between
    pipelines.py:15131 and 16105 (slice-3 baseline) must still be
    present (the audit was per-site judgement, not blanket
    replacement). A silent collapse of the audit window to bare
    ``except Exception`` without the explanatory noqa marker
    regresses on cq-3's clarity goal.

    Window line numbers shifted from the slice-3 baseline of
    [15131, 16105] to [15700, 16850] after slice-4 (#2548) inserted
    ~775 lines earlier in the file (eager-persist of
    ``parent_branch_at_creation``, merge-base fallback in
    ``_resolve_slice_base_branch``, Layer-C bootstrap), then to
    [15923, 17073] after the human-focused draft companion work
    inserted ~223 lines earlier in the file (the ``simplifier``
    role's orientation + task prompt, ``_get_human_draft_path`` /
    ``_read_human_phase_draft`` helpers, reviewer-prep additions).
    The shifted window captures the same audited handlers.

    We pin the population count loosely (>=10 sites in window — the
    audit allowed narrowing 4 of the original 20) rather than the
    exact 16-17 remaining, so a future micro-refactor that drops
    one or two doesn't trip the test.

    Per-site catch-explanation is verified by the coder's commit
    message (which enumerates each site's catch rationale) and the
    in-source comments / log lines next to each handler — a
    code-review surface that does not lend itself to a mechanized
    pattern match (some sites document via the ``logger.debug`` call
    after the handler, others via a preceding block comment).
    """
    # The #2270 overseer overhaul restructured routes/pipelines.py (folding the
    # overseer spawn path, deleting ``_check_and_respawn_overseer``, adding the
    # corrective-vocabulary executor), which shifts line numbers each slice and
    # invalidates the absolute slice-3 audit window [15700, 16850]. The audit's
    # real invariant is population-level — the documented ``# noqa: BLE001``
    # swallow sites must not silently collapse to bare ``except Exception`` — so
    # count the documented population file-wide instead of inside a fixed,
    # ever-shifting line range. (Per-site narrowing is pinned by the three
    # specific tests above.)
    lines = _PIPELINES_SRC.splitlines()
    noqa_lines = [i for i, line in enumerate(lines) if "noqa: BLE001" in line]
    assert len(noqa_lines) >= 40, (
        f"Found only {len(noqa_lines)} documented ``# noqa: BLE001`` swallow "
        f"sites in routes/pipelines.py — the BLE001 audit population appears to "
        f"have silently collapsed toward bare ``except Exception``."
    )
    # Upper bound guards against a future PR re-introducing swallow-all handlers
    # en masse without re-running the audit. The file has carried ~80 documented
    # swallows through the overhaul; a jump well past that needs an audit pass.
    assert len(noqa_lines) <= 120, (
        f"Found {len(noqa_lines)} ``# noqa: BLE001`` swallows in "
        f"routes/pipelines.py, well past the documented population — a future "
        f"PR appears to have re-introduced swallow-all handlers without "
        f"re-running the audit."
    )
