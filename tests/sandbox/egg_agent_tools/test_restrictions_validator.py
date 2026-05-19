"""Tests for the agent-side policy enforcement (#2717 slice-2 task-2-6).

Contingency context (slice-1 R2 verdict)
----------------------------------------

Slice-1's TASK-1-5 ran the R2 nested-dispatch spike and wrote the
verdict to ``.egg-state/<pipeline_id>/r2-verdict.json``. The
slice-1 BRC history records the **verdict = "pass"**: the
PreToolUse hook correctly resolves the child role under nested
dispatch (parent=architect + child=tester writing
``orchestrator/foo.py`` → ``decision=block`` with a tester-naming
reason; the cross-role probe, in-role negative-control, and
EGG_AGENT_ROLE leak guard all pass).

Per the contract task-2-5 description:

    If R2 = pass, this task is a no-op (close with note). Tests for
    this code path land in TASK-2-6 (tester-owned).

And task-2-6's acceptance criterion:

    (R2 pass) asserts the validator helper is a no-op for
    in-allow-list writes (the contingency is documented in the test
    docstring).

So this file's job is the **no-op regression guard**: assert that
``check_file_restriction`` continues to return the gateway-shape
response for in-allow-list writes — i.e., the slice-2 work did NOT
silently extend the in-sandbox handler with R2-fail-only enforcement
logic and accidentally change the response shape for the R2-pass
path. The PreToolUse hook (orchestrator/substrate/claude_code/
hook_entry.py) remains the load-bearing enforcement seam; the
in-sandbox ``restrictions`` handler stays a pure-read self-check.

What this test enforces
-----------------------

1. **In-allow-list write — response shape stable.** When a role's
   own pattern matches the requested path, ``check_file_restriction``
   returns ``can_write=True`` with the documented gateway-shape
   fields (``ok``, ``role``, ``path``, ``can_write``, ``reason``,
   ``alternative_role``). No new fields, no removed fields, no
   shape drift introduced by the slice-2 work.

2. **Cross-role write — denial-shape stable.** When the role cannot
   write the path, the response carries ``can_write=False``,
   ``reason`` references ``shared/egg_restrictions/patterns.py``,
   and ``alternative_role`` is populated when exactly one producer
   role covers the path.

3. **No new validator surface.** Slice-2 must not introduce a new
   ``validate_write_target`` (or any other) symbol on the
   ``restrictions`` handler module that would constitute the
   R2-fail enforcement path. If such a symbol appears it would be a
   sign that the cq-6 option-2 work landed without being needed —
   surface that as a soft heads-up via a clearly-named test.

If slice-1's R2 verdict were instead ``"fail"`` the contingent
TASK-2-5 would have landed a validator and this file would need
positive coverage of the denial path. That path is not in scope
because R2 passed. If a future slice flips R2 to fail (the cq-3
deferral makes that possible), this test will need a sibling that
exercises the new validator's denial shape — captured in the test
docstring per the acceptance criterion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure sandbox / shared are importable. Mirrors the pattern from
# ``tests/sandbox/egg_agent_tools/test_handlers_sdlc.py``.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import restrictions  # noqa: E402
from egg_agent_tools.handlers.errors import HandlerError  # noqa: E402

# ---------------------------------------------------------------------------
# Documented gateway-shape fields per ``check_file_restriction``'s
# docstring. Pin them as a frozenset so a shape drift fails clearly
# with a missing-key / extra-key message.
# ---------------------------------------------------------------------------

_SINGLE_PATH_FIELDS: frozenset[str] = frozenset(
    {"ok", "role", "path", "can_write", "reason", "alternative_role"}
)


# ---------------------------------------------------------------------------
# In-allow-list writes — the R2-pass no-op invariant
# ---------------------------------------------------------------------------


def test_coder_in_allow_list_response_shape_stable() -> None:
    """``check_file_restriction`` returns the documented shape for an in-allow-list write.

    The R2-pass no-op invariant: the slice-2 work must NOT extend
    ``check_file_restriction``'s in-allow-list response with new
    fields or change ``can_write`` to anything other than ``True``
    for a path the role's pattern allows.

    A coder writing under ``orchestrator/`` is the canonical
    in-allow-list case (per ``shared/egg_restrictions/patterns.py``).
    """
    req = {"role": "coder", "path": "orchestrator/foo.py"}
    resp = restrictions.check_file_restriction(req)

    assert resp["ok"] is True
    assert resp["role"] == "coder"
    assert resp["path"] == "orchestrator/foo.py"
    assert resp["can_write"] is True, (
        f"coder writing orchestrator/foo.py must be allowed; got "
        f"can_write={resp.get('can_write')!r}. If this fails, either "
        f"the pattern registry changed shape (file a follow-up) or the "
        f"slice-2 work accidentally introduced enforcement that the "
        f"R2-pass verdict said wasn't needed."
    )
    assert isinstance(resp.get("reason"), str), (
        f"``reason`` must be a string even on the allowed path; got {resp.get('reason')!r}"
    )
    assert resp.get("alternative_role") is None, (
        f"``alternative_role`` must be None on the allowed path "
        f"(it only names the alternative producer role on denial); "
        f"got {resp.get('alternative_role')!r}"
    )
    # No extra keys leaked into the response shape.
    assert set(resp.keys()) == _SINGLE_PATH_FIELDS, (
        f"in-allow-list response shape must equal "
        f"{sorted(_SINGLE_PATH_FIELDS)}; got "
        f"{sorted(resp.keys())}. The R2-pass no-op invariant requires "
        f"that slice-2 NOT introduce new fields in the validator's "
        f"response shape."
    )


def test_tester_in_allow_list_response_shape_stable() -> None:
    """``check_file_restriction`` returns the documented shape for a tester writing under tests/.

    Tester under tests/ is the canonical in-allow-list case for the
    tester role per the gateway pattern registry. Pinning both the
    coder and the tester cases catches regressions that only one of
    them surfaces.
    """
    req = {"role": "tester", "path": "tests/sandbox/egg_agent_tools/test_x.py"}
    resp = restrictions.check_file_restriction(req)

    assert resp["ok"] is True
    assert resp["role"] == "tester"
    assert resp["can_write"] is True, (
        f"tester writing tests/sandbox/egg_agent_tools/test_x.py must "
        f"be allowed; got can_write={resp.get('can_write')!r}"
    )
    assert resp.get("alternative_role") is None
    assert set(resp.keys()) == _SINGLE_PATH_FIELDS


def test_documenter_in_allow_list_response_shape_stable() -> None:
    """``check_file_restriction`` returns the documented shape for documenter under docs/."""
    req = {"role": "documenter", "path": "docs/foo.md"}
    resp = restrictions.check_file_restriction(req)

    assert resp["ok"] is True
    assert resp["role"] == "documenter"
    assert resp["can_write"] is True, (
        f"documenter writing docs/foo.md must be allowed; got can_write={resp.get('can_write')!r}"
    )
    assert set(resp.keys()) == _SINGLE_PATH_FIELDS


# ---------------------------------------------------------------------------
# Cross-role denial — the slice-1 PreToolUse-hook path stays the
# enforcement seam; the validator's denial shape must remain stable.
# ---------------------------------------------------------------------------


def test_coder_cannot_write_tester_path_denial_shape_stable() -> None:
    """Cross-role denial: coder cannot write ``tests/*``; alternative_role names tester.

    Pinned so a slice-2 regression that changed the denial's
    ``reason`` text to drop the ``shared/egg_restrictions/patterns.py``
    pointer (or stripped the ``alternative_role`` field) surfaces
    here, not at gateway-403 time.
    """
    req = {"role": "coder", "path": "tests/sandbox/egg_agent_tools/test_x.py"}
    resp = restrictions.check_file_restriction(req)

    assert resp["ok"] is True
    assert resp["role"] == "coder"
    assert resp["can_write"] is False, (
        f"coder writing tests/ must be denied; got can_write={resp.get('can_write')!r}"
    )
    assert "shared/egg_restrictions/patterns.py" in resp.get("reason", ""), (
        f"denial reason must reference the pattern registry; got {resp.get('reason')!r}"
    )
    assert resp.get("alternative_role") == "tester", (
        f"``alternative_role`` must name tester when coder is blocked "
        f"from a tests/ path; got {resp.get('alternative_role')!r}. "
        f"Without this the impasse-routing path can't auto-delegate."
    )
    assert set(resp.keys()) == _SINGLE_PATH_FIELDS


def test_tester_cannot_write_orchestrator_path_denial_shape_stable() -> None:
    """Cross-role denial: tester cannot write ``orchestrator/*``; alternative_role names coder."""
    req = {"role": "tester", "path": "orchestrator/foo.py"}
    resp = restrictions.check_file_restriction(req)

    assert resp["ok"] is True
    assert resp["role"] == "tester"
    assert resp["can_write"] is False, (
        f"tester writing orchestrator/foo.py must be denied; got "
        f"can_write={resp.get('can_write')!r}"
    )
    assert "shared/egg_restrictions/patterns.py" in resp.get("reason", "")
    assert resp.get("alternative_role") == "coder", (
        f"``alternative_role`` must name coder when tester is blocked "
        f"from an orchestrator/ path; got {resp.get('alternative_role')!r}"
    )


# ---------------------------------------------------------------------------
# Negative invariant — no new validator surface was added on the
# R2-pass path.
# ---------------------------------------------------------------------------


def test_no_new_validator_symbol_introduced_in_r2_pass_slice() -> None:
    """The slice-2 work must NOT introduce a ``validate_write_target`` (or peer) symbol.

    The R2-pass no-op invariant: TASK-2-5 said "If R2 = pass, this
    task is a no-op (close with note)." If a symbol like
    ``validate_write_target`` appears on the ``restrictions``
    handler module it would mean the R2-fail enforcement path landed
    despite the verdict — surface that here so the slice-1 R2
    verdict and the slice-2 implementation stay consistent.

    Test is informational on a green run (the symbol is absent) and
    fires loudly on a regression. Distinct from the per-test
    assertions above so the failure mode is easy to triage.
    """
    forbidden = {"validate_write_target"}
    leaked = {name for name in forbidden if hasattr(restrictions, name)}
    assert not leaked, (
        f"R2-pass no-op invariant violated: slice-2 added unexpected "
        f"symbol(s) {sorted(leaked)} to "
        f"sandbox/egg_agent_tools/handlers/restrictions.py. The slice-1 "
        f"R2 verdict was ``pass`` so the agent-side enforcement path "
        f"(cq-6 option 2 from #2623) should NOT have landed. Either "
        f"(a) the R2 verdict flipped to ``fail`` and TASK-2-6 should "
        f"now cover the positive denial path, or (b) the slice-2 work "
        f"accidentally landed enforcement code that needs to be "
        f"reverted."
    )


# ---------------------------------------------------------------------------
# Adversarial probes — even on the no-op path, the validator's
# defensive surface must hold.
# ---------------------------------------------------------------------------


def test_missing_path_raises_handler_error() -> None:
    """Calling ``check_file_restriction`` without ``path`` raises HandlerError.

    Defensive invariant: a slice-2 regression that silently
    swallowed the missing-arg case (e.g., by short-circuiting on the
    R2-pass branch before validation ran) would be a security risk —
    an agent could pass an empty request and get a falsy
    ``can_write`` answer without the validator ever inspecting the
    real path.
    """
    with pytest.raises(HandlerError, match="'path' is required"):
        restrictions.check_file_restriction({"role": "coder"})


def test_unknown_role_raises_handler_error() -> None:
    """Unknown role surfaces as ``HandlerError`` (not ``can_write=True``).

    Slice-2 must not introduce a fall-through that maps an unknown
    role to a permissive answer. Pin the existing behaviour so a
    regression that loosens role validation surfaces here.

    Reviewer_code v2 non-blocking N11: this test previously patched
    ``restrictions.get_agent_role`` to return ``None``, but
    ``check_file_restriction`` short-circuits on the truthy
    ``req["role"] = "unknown_xyz"`` (``role = req.get("role") or
    get_agent_role()``), so the patch never fired. The patch is
    dropped to make the test's intent unambiguous.
    """
    with pytest.raises(HandlerError):
        restrictions.check_file_restriction({"role": "unknown_xyz", "path": "orchestrator/foo.py"})


def test_list_path_returns_per_path_results() -> None:
    """When ``path`` is a list, the validator returns a per-path ``results`` array.

    Pin the bulk-check surface so a regression that flattened the
    response to a single answer surfaces here. The bulk surface is
    documented in ``check_file_restriction``'s docstring and is the
    shape the orchestrator's impasse-routing relies on when a task
    names multiple ``blocked_files``.
    """
    req = {
        "role": "coder",
        "path": ["orchestrator/foo.py", "tests/test_x.py"],
    }
    resp = restrictions.check_file_restriction(req)

    assert resp["ok"] is True
    assert resp["role"] == "coder"
    assert "results" in resp, (
        f"list-shaped path must return ``results``; got keys {sorted(resp.keys())}"
    )
    assert len(resp["results"]) == 2
    # First path is in-allow-list, second is denied.
    assert resp["results"][0]["can_write"] is True
    assert resp["results"][1]["can_write"] is False
    assert resp["results"][1]["alternative_role"] == "tester"
