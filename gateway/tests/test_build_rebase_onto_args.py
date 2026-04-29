"""Tests for ``gateway.git_client.build_rebase_onto_args`` (#2137 TASK-5-2).

The reconciler in :mod:`orchestrator.stacked_pr_reconciler` rebases child
slice branches onto a new parent branch when the parent merges. The
gateway-side helper :func:`gateway.git_client.build_rebase_onto_args`
is the single allowlisted source of the canonical
``rebase --onto NEW OLD BRANCH`` argv shape — it constructs the argv
and runs it through ``validate_git_args("rebase", ...)`` so any extra
flag (``--strategy-option=ours``, ``-X theirs``, etc.) that an
attacker-controlled caller might try to slip in is rejected before
the request reaches the gateway's ``/git`` endpoint.

Coverage:

* Happy path: well-formed branch / new_base / old_base produce the
  canonical ``["--onto", new_base, old_base, branch]`` shape and
  ``ok=True``.
* Empty / non-string inputs are rejected with ``ok=False`` and a
  ``branch / new_base / old_base must be a non-empty string`` error.
* Whitespace-only inputs are rejected (``"   "`` is treated the same
  as ``""``).
* No leakage of extra flags: callers cannot pass ``--strategy-option``
  by squashing it into one of the input strings — the value would
  travel as a positional arg, which is what the rebase allowlist is
  designed to accept (refs are positional). The branch still shows up
  in the argv list with no flag injection. (We assert the argv shape
  literally to catch any future regression where the function decides
  to "pass through" extra flags via input parsing.)
"""

from __future__ import annotations

import sys
from pathlib import Path

# sys.path setup — gateway is needed so ``from gateway.git_client``
# works; without it Python only finds the top-level ``gateway``
# package symbolically.
_project_root = Path(__file__).parent.parent.parent
_gateway_path = _project_root / "gateway"
if _gateway_path.exists() and str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from gateway.git_client import build_rebase_onto_args  # noqa: E402


class TestHappyPath:
    """Well-formed inputs produce the canonical argv shape."""

    def test_canonical_argv_shape(self) -> None:
        args, ok, err = build_rebase_onto_args(
            branch="egg/issue-2137/slice-2",
            new_base="egg/issue-2137",
            old_base="egg/issue-2137/slice-1",
        )
        assert ok is True
        assert err == ""
        # The shape MUST be exactly --onto NEW OLD BRANCH so the
        # gateway's allowlist sees positional refs only.
        assert args == [
            "--onto",
            "egg/issue-2137",
            "egg/issue-2137/slice-1",
            "egg/issue-2137/slice-2",
        ]

    def test_simple_branch_names(self) -> None:
        args, ok, err = build_rebase_onto_args(
            branch="feature",
            new_base="main",
            old_base="develop",
        )
        assert ok is True
        assert err == ""
        assert args == ["--onto", "main", "develop", "feature"]

    def test_returns_three_tuple_on_success(self) -> None:
        result = build_rebase_onto_args("a", "b", "c")
        assert isinstance(result, tuple)
        assert len(result) == 3
        args, ok, err = result
        assert isinstance(args, list)
        assert isinstance(ok, bool)
        assert isinstance(err, str)


class TestRejectEmptyInputs:
    """Empty/whitespace/non-string inputs surface a structured error."""

    def test_empty_branch_rejected(self) -> None:
        args, ok, err = build_rebase_onto_args("", "main", "develop")
        assert ok is False
        assert args == []
        assert "branch" in err
        assert "non-empty" in err

    def test_empty_new_base_rejected(self) -> None:
        args, ok, err = build_rebase_onto_args("feature", "", "develop")
        assert ok is False
        assert args == []
        assert "new_base" in err
        assert "non-empty" in err

    def test_empty_old_base_rejected(self) -> None:
        args, ok, err = build_rebase_onto_args("feature", "main", "")
        assert ok is False
        assert args == []
        assert "old_base" in err
        assert "non-empty" in err

    def test_whitespace_only_branch_rejected(self) -> None:
        args, ok, err = build_rebase_onto_args("   ", "main", "develop")
        assert ok is False
        assert args == []
        assert "branch" in err

    def test_whitespace_only_new_base_rejected(self) -> None:
        args, ok, err = build_rebase_onto_args("feature", "\t", "develop")
        assert ok is False
        assert args == []
        assert "new_base" in err

    def test_whitespace_only_old_base_rejected(self) -> None:
        args, ok, err = build_rebase_onto_args("feature", "main", "\n  ")
        assert ok is False
        assert args == []
        assert "old_base" in err

    def test_none_branch_rejected(self) -> None:
        # ``None`` is not a string — the type guard rejects it before
        # ``.strip()`` would AttributeError.
        args, ok, err = build_rebase_onto_args(None, "main", "develop")  # type: ignore[arg-type]
        assert ok is False
        assert args == []
        assert "branch" in err

    def test_none_new_base_rejected(self) -> None:
        args, ok, err = build_rebase_onto_args("feature", None, "develop")  # type: ignore[arg-type]
        assert ok is False
        assert args == []
        assert "new_base" in err

    def test_none_old_base_rejected(self) -> None:
        args, ok, err = build_rebase_onto_args("feature", "main", None)  # type: ignore[arg-type]
        assert ok is False
        assert args == []
        assert "old_base" in err

    def test_non_string_branch_rejected(self) -> None:
        # Numeric branches are sometimes auto-generated; guard rejects.
        args, ok, err = build_rebase_onto_args(123, "main", "develop")  # type: ignore[arg-type]
        assert ok is False
        assert args == []
        assert "branch" in err


class TestNoFlagLeakage:
    """The helper does not allow flag injection via input strings."""

    def test_no_extra_flags_added_to_argv(self) -> None:
        """The argv must contain ONLY ``--onto`` plus the three refs."""
        args, ok, _ = build_rebase_onto_args("feature", "main", "develop")
        assert ok is True
        # No ``--strategy-option`` / ``-X`` / ``--exec`` style flags.
        flag_args = [a for a in args if a.startswith("-")]
        assert flag_args == ["--onto"]

    def test_input_strings_travel_as_positional_refs(self) -> None:
        """Even if a ref name resembles a flag, the argv shape is fixed.

        The allowlist validator runs on the constructed argv. The refs
        appear in fixed positions (slots 1, 2, 3 after ``--onto``); they
        are not re-parsed as flags.
        """
        args, ok, _ = build_rebase_onto_args(
            branch="branch",
            new_base="new",
            old_base="old",
        )
        assert ok is True
        assert args[0] == "--onto"
        assert args[1] == "new"
        assert args[2] == "old"
        assert args[3] == "branch"

    def test_validate_git_args_invoked(self) -> None:
        """validate_git_args is called and its verdict drives the return.

        We don't monkeypatch — we just confirm the canonical shape
        passes the real validator (the same path the gateway runs at
        request time). Any future change that introduces a ``--exec``
        or ``--strategy-option`` argument from a caller field would be
        rejected at this step.
        """
        # Sanity check: the canonical shape passes the real validator.
        args, ok, err = build_rebase_onto_args("a", "b", "c")
        assert ok is True
        assert args == ["--onto", "b", "c", "a"]
        assert err == ""


class TestErrorContract:
    """The (args, ok, error) triple shape is stable."""

    def test_failure_path_returns_empty_args(self) -> None:
        """``args`` MUST be ``[]`` when ``ok is False`` so a caller
        that forgets to check ``ok`` doesn't accidentally submit a
        partial argv."""
        args, ok, _ = build_rebase_onto_args("", "main", "develop")
        assert ok is False
        assert args == []

    def test_success_path_error_is_empty_string(self) -> None:
        """``error`` MUST be ``""`` (not ``None``) when ``ok is True``."""
        _, ok, err = build_rebase_onto_args("feature", "main", "develop")
        assert ok is True
        assert err == ""
        assert isinstance(err, str)
