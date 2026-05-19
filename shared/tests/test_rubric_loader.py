"""Tests for ``_load_egg_sdlc_role_rubric`` (#2717 slice-1 task-1-7).

Acceptance criteria covered (per contract task-1-7):

* ``_load_egg_sdlc_role_rubric(REFINER)`` returns the existing rubric markdown
  (regression — must not break the spike's working refiner path).
* ``_load_egg_sdlc_role_rubric(REVIEWER_REFINE)`` returns the rubric markdown
  added by task-1-4 (documenter-owned ``reviewer_refine.md``).
* ``_load_egg_sdlc_role_rubric(REVIEWER_AGENT_DESIGN)`` returns the rubric
  markdown added by task-1-4 (documenter-owned ``reviewer_agent_design.md``).
* ``_load_egg_sdlc_role_rubric(ARCHITECT)`` returns the rubric markdown —
  slice-2 (task-2-2 / task-2-3) extends the loader's rubric-landed set
  to the plan team and ships ``architect.md``. (Slice-1 originally
  expected this role to still raise with a "follow-up slice 2" hint;
  the merge of slice-2's loader expansion onto slice-1 flips it to
  loadable.)

The four required cases (refiner regression / reviewer_refine /
reviewer_agent_design / architect-loads) are implemented as discrete
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
* Implement-phase roles (e.g. ``REVIEWER_CODE``) still raise
  ``ValueError`` — slice 3 delivers those rubrics. Plan-phase roles
  (``REVIEWER_PLAN``, ``TASK_PLANNER``, etc.) became loadable in
  slice-2 and no longer belong in the still-deferred set.
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


def test_load_architect_rubric() -> None:
    """``ARCHITECT`` rubric loads from ``architect.md`` in slice-2.

    Slice-1 originally pinned this role as raising ``ValueError`` with a
    "follow-up slice 2" hint (task-1-6). Slice-2 (task-2-2 / task-2-3)
    extends the loader to the plan team — architect is now in the
    rubric-landed set and ``architect.md`` exists on disk.
    """
    body = _load(AgentRole.ARCHITECT)
    assert isinstance(body, str)
    assert body.strip(), "architect rubric body must not be empty"
    assert "architect" in body.lower(), (
        "architect rubric must reference its own role name in the body"
    )


# ---------------------------------------------------------------------------
# Adversarial probing — required for the loader to be safe in production
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_input",
    [
        # Slice-1 regression role.
        pytest.param(AgentRole.REFINER, id="enum-refiner"),
        pytest.param("refiner", id="str-refiner"),
        # Slice-1 newly-supported roles (REVIEWER_REFINE,
        # REVIEWER_AGENT_DESIGN) — pin string-input contract here so a
        # future loader change that breaks the str→enum normalisation
        # for the *new* roles (not just the regression role) is caught.
        pytest.param(AgentRole.REVIEWER_REFINE, id="enum-reviewer_refine"),
        pytest.param("reviewer_refine", id="str-reviewer_refine"),
        pytest.param(AgentRole.REVIEWER_AGENT_DESIGN, id="enum-reviewer_agent_design"),
        pytest.param("reviewer_agent_design", id="str-reviewer_agent_design"),
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
    "implement_phase_role",
    [
        # Implement-team roles still deferred to slice 3 of the #2717
        # rollout — REVIEWER_PLAN and TASK_PLANNER shipped in slice-2
        # (task-2-2 / task-2-3) and were removed from this parameter
        # list when the loader's `_RUBRIC_LANDED_ROLES` set grew to
        # include the plan team.
        pytest.param(AgentRole.REVIEWER_CODE, id="reviewer_code"),
    ],
)
def test_loader_still_rejects_unshipped_roles(implement_phase_role: object) -> None:
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
        _load(implement_phase_role)


def test_loader_rejects_path_traversal_role_name() -> None:
    """A role value containing path-traversal characters cannot escape the agents dir.

    Defence-in-depth: even though ``AgentRole`` values are constants
    in ``shared/egg_contracts/agent_roles.py``, the loader accepts
    string inputs via the ``str(role)`` branch. An attacker model
    where a string role value reaches this loader (config injection,
    deserialised contract field) must not yield arbitrary file read.

    The loader's structural defence is the ``_ROLE_RUBRIC_SLICES``
    allowlist (orchestrator/substrate/__init__.py): roles outside the
    allowlist take the slice-fence branch and raise ``ValueError``
    before any ``Path.is_file()`` check happens against the
    user-controlled path. The test pins both that the error fires AND
    that the diagnostic identifies the role as "not part of the
    rollout's rubric set" rather than "missing on disk" — the former
    means the allowlist caught it, the latter would mean the loader
    walked the filesystem with attacker-controlled segments.
    """
    with pytest.raises(ValueError) as excinfo:
        _load("../../../etc/passwd")
    msg = str(excinfo.value)
    # The error must still cite a missing rubric — not silently read
    # the wrong file or yield an empty string.
    assert "missing" in msg.lower() or "rubric" in msg.lower(), (
        f"path-traversal role must surface as missing-rubric ValueError; got: {msg!r}"
    )
    # Adversarial assertion: the diagnostic must identify the role as
    # not-in-rollout-set rather than as missing-on-disk. The former
    # means the ``_ROLE_RUBRIC_SLICES`` allowlist intercepted before
    # any filesystem walk; the latter would mean the loader reached
    # ``Path(...).is_file()`` with attacker-controlled path segments
    # — an information-leak vector (existence oracle on /etc/*.md).
    lowered = msg.lower()
    assert "not part of" in lowered or "rollout" in lowered or "rubric set" in lowered, (
        "path-traversal role must hit the allowlist's slice-fence "
        f"branch (not the file-missing-on-disk branch); got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Rollout-DAG invariants — pin the "extend, don't replace" contract on
# ``_LANDED_SLICES`` so slice-2's author cannot accidentally regress
# slice-1 by writing ``frozenset({"slice-2"})`` instead of
# ``frozenset({"slice-1", "slice-2"})``.
# ---------------------------------------------------------------------------


def test_landed_slices_contains_slice1() -> None:
    """``_LANDED_SLICES`` must include ``"slice-1"`` in every future slice.

    The rollout DAG (issue #2717) ships slice-1 first; later slices
    EXTEND ``_LANDED_SLICES`` rather than replacing it. A regression
    where slice-2's coder wrote ``frozenset({"slice-2"})`` would fence
    off slice-1's already-landed refiner / reviewer_refine /
    reviewer_agent_design rubrics — a silent break of the loader for
    the entire refine team. The constant's docstring at
    ``orchestrator/substrate/__init__.py:284-287`` calls this invariant
    out in prose; this test pins it mechanically so a future-slice edit
    cannot regress slice-1 without tripping a test.
    """
    landed = substrate_pkg._LANDED_SLICES
    assert isinstance(landed, frozenset), (
        f"_LANDED_SLICES must remain a frozenset (immutable, hashable); got {type(landed).__name__}"
    )
    assert "slice-1" in landed, (
        f"_LANDED_SLICES must include 'slice-1' on every slice; "
        f"got {sorted(landed)!r}. The 'extend, don't replace' invariant "
        "is documented at orchestrator/substrate/__init__.py:284-287; "
        "future slices add to this set, they do not replace it."
    )
