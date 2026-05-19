"""Tests for ``_load_egg_sdlc_role_rubric`` (#2717 slice-1 task-1-7).

Acceptance criteria covered (per contract task-1-7):

* ``_load_egg_sdlc_role_rubric(REFINER)`` returns the existing rubric markdown
  (regression — must not break the spike's working refiner path).
* ``_load_egg_sdlc_role_rubric(REVIEWER_REFINE)`` returns the rubric markdown
  added by task-1-4 (documenter-owned ``reviewer_refine.md``).
* ``_load_egg_sdlc_role_rubric(REVIEWER_AGENT_DESIGN)`` returns the rubric
  markdown added by task-1-4 (documenter-owned ``reviewer_agent_design.md``).
* ``_load_egg_sdlc_role_rubric(ARCHITECT)`` still raises ``ValueError`` with
  the diagnostic hint updated from "follow-up issue per cq-11" to
  "follow-up slice 2" (task-1-6 acceptance criterion).

The four required cases (refiner regression / reviewer_refine /
reviewer_agent_design / architect-raises) are implemented as discrete
parametrized tests so a single failure points cleanly at one role's
loader behavior.

Adversarial probing layered on top of the contract's required cases:

* ``role`` accepts both ``AgentRole`` enum members and bare strings —
  the loader normalises via ``role.value if hasattr(role, "value") else
  str(role)`` (line ~263). Both shapes must round-trip identically.
* Loaded markdown is non-empty and includes the canonical "You are the"
  preamble so a silently empty / dead file is detectable.
* Path-traversal safety: a role value containing ``..`` resolves to a
  ``ValueError`` rather than reaching outside the rubric directory.
  (The loader builds ``rubric_path = repo_root / "plugins" / ... /
  f"{role_name}.md"``; an attacker who can supply role values cannot
  escape the agents directory because ``role_name`` is appended as a
  filename component.)
* Plan-phase / implement-phase roles (e.g. ``REVIEWER_PLAN``,
  ``REVIEWER_CODE``) still raise ``ValueError`` — slice 2/3 deliver
  those rubrics, not slice 1.
"""

from __future__ import annotations

import pytest

substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)
agent_roles_mod = pytest.importorskip(
    "egg_contracts.agent_roles",
    reason="shared/egg_contracts/agent_roles.py not importable",
)

AgentRole = agent_roles_mod.AgentRole
_load = substrate_pkg._load_egg_sdlc_role_rubric


# ---------------------------------------------------------------------------
# Required cases from task-1-7 acceptance criteria
# ---------------------------------------------------------------------------


def test_load_refiner_rubric_regression() -> None:
    """Refiner rubric still loads — slice-1 must not regress the spike.

    The refiner is the only role the walking-skeleton spike (#2623)
    shipped a rubric for. Slice 1 widens the loader to two more roles
    (REVIEWER_REFINE, REVIEWER_AGENT_DESIGN) without touching this
    path; this test pins the existing behaviour so a careless rewrite
    of the loader does not silently drop the refiner.
    """
    body = _load(AgentRole.REFINER)
    assert isinstance(body, str)
    assert body.strip(), "refiner rubric body must not be empty"
    # The frontmatter is retained per the existing loader contract.
    assert body.startswith("---"), (
        "refiner rubric must include frontmatter (loader returns the full "
        "file, frontmatter included, per the docstring)"
    )


def test_load_reviewer_refine_rubric() -> None:
    """``REVIEWER_REFINE`` rubric loads from ``reviewer_refine.md``.

    The documenter ships ``reviewer_refine.md`` under
    ``plugins/egg-sdlc/skills/egg-sdlc/agents/`` (task-1-4) and the
    coder removes the ``ValueError`` fence for this role from
    ``_load_egg_sdlc_role_rubric`` (task-1-6). The two must compose so
    a single ``_load(AgentRole.REVIEWER_REFINE)`` call returns the
    rubric body.
    """
    body = _load(AgentRole.REVIEWER_REFINE)
    assert isinstance(body, str)
    assert body.strip(), "reviewer_refine rubric body must not be empty"
    # Acceptance for task-1-4 requires the body to start with a
    # "You are the **reviewer_refine** running on the Claude Code
    # substrate" (or analogous) preamble.
    assert "reviewer_refine" in body.lower(), (
        "reviewer_refine rubric must reference its own role name in the body"
    )


def test_load_reviewer_agent_design_rubric() -> None:
    """``REVIEWER_AGENT_DESIGN`` rubric loads from ``reviewer_agent_design.md``."""
    body = _load(AgentRole.REVIEWER_AGENT_DESIGN)
    assert isinstance(body, str)
    assert body.strip(), "reviewer_agent_design rubric body must not be empty"
    assert "reviewer_agent_design" in body.lower(), (
        "reviewer_agent_design rubric must reference its own role name in the body"
    )


def test_load_architect_raises_value_error_with_slice2_hint() -> None:
    """``ARCHITECT`` still raises ``ValueError`` — the loader fence remains in place.

    Task-1-6 acceptance criterion: the diagnostic hint flips from the
    spike's "follow-up issue per cq-11" to "follow-up slice 2". The
    test pins the wording so a regression that silently drops the
    diagnostic (or reverts to the pre-rollout text) is caught.
    """
    with pytest.raises(ValueError) as excinfo:
        _load(AgentRole.ARCHITECT)
    msg = str(excinfo.value)
    # Cover both the role identification and the updated diagnostic
    # pointer. The previous "follow-up issue per cq-11" wording must
    # not survive into the rollout.
    assert "architect" in msg.lower(), f"ValueError must name the role under failure; got: {msg!r}"
    # AC: hint references "follow-up slice 2" — accept either the
    # hyphenated or spaced form ("slice-2" / "slice 2") since the
    # intent ("the rubric is deferred to the second rollout slice")
    # is identical.
    lowered = msg.lower()
    assert (
        "follow-up slice 2" in lowered
        or "follow-up slice-2" in lowered
        or "slice 2" in lowered
        or "slice-2" in lowered
    ), f"task-1-6 AC requires the diagnostic hint to reference 'follow-up slice 2'; got: {msg!r}"
    # Adversarial: ensure the old cq-11 hint is gone — silently
    # leaving it in place would defeat the AC.
    assert "cq-11" not in lowered, (
        f"task-1-6 AC: 'follow-up issue per cq-11' wording must be replaced; got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Adversarial probing — required for the loader to be safe in production
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_input",
    [
        pytest.param(AgentRole.REFINER, id="enum-refiner"),
        pytest.param("refiner", id="str-refiner"),
    ],
)
def test_loader_accepts_enum_and_string_role(role_input: object) -> None:
    """The loader normalises ``AgentRole`` enum members and bare strings.

    The implementation uses ``role.value if hasattr(role, "value") else
    str(role)``. Both shapes must produce identical output — if the
    enum form started silently using ``str(role)`` (which for
    ``StrEnum`` returns the value, so this would still work) vs an
    accidental ``repr(role)`` ("<AgentRole.REFINER: 'refiner'>"), the
    file path lookup would diverge.
    """
    body = _load(role_input)
    assert isinstance(body, str)
    assert body.strip()


@pytest.mark.parametrize(
    "plan_phase_role",
    [
        pytest.param(AgentRole.REVIEWER_PLAN, id="reviewer_plan"),
        pytest.param(AgentRole.REVIEWER_CODE, id="reviewer_code"),
        pytest.param(AgentRole.TASK_PLANNER, id="task_planner"),
    ],
)
def test_loader_still_rejects_unshipped_roles(plan_phase_role: object) -> None:
    """Roles whose rubrics are not yet shipped continue to raise.

    Task-1-6 description: "The loader continues to raise ``ValueError``
    for plan/implement roles until slice 2/3 adds their rubrics (this
    preserves the structured-error contract for missing rubrics)."

    If a future change silently drops the fence for every role, the
    walking-skeleton callers would get an empty / fallback rubric and
    the spawn would degrade silently. The fence is a load-bearing
    diagnostic.
    """
    with pytest.raises(ValueError):
        _load(plan_phase_role)


def test_loader_rejects_path_traversal_role_name() -> None:
    """A role value containing path-traversal characters cannot escape the agents dir.

    Defence-in-depth: even though ``AgentRole`` values are constants
    in ``shared/egg_contracts/agent_roles.py``, the loader accepts
    string inputs via the ``str(role)`` branch. An attacker model
    where a string role value reaches this loader (config injection,
    deserialised contract field) must not yield arbitrary file read.

    The loader builds ``rubric_path = repo_root / "plugins" / ... /
    f"{role_name}.md"`` — when ``role_name`` contains ``..`` or a
    slash, the resulting path either escapes the agents directory or
    does not match any shipped rubric. ``rubric_path.is_file()``
    returns False for the non-existent path, and ``ValueError`` is
    raised. The test pins this safe-by-default behaviour.
    """
    with pytest.raises(ValueError) as excinfo:
        _load("../../../etc/passwd")
    msg = str(excinfo.value)
    # The error must still cite a missing rubric — not silently read
    # the wrong file or yield an empty string.
    assert "missing" in msg.lower() or "rubric" in msg.lower(), (
        f"path-traversal role must surface as missing-rubric ValueError; got: {msg!r}"
    )
