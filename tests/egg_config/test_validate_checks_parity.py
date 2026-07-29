"""Parity tests pinning the three copies of ``validate_checks``.

``egg_config.validators.validate_checks`` is the canonical
implementation. ``config/repo_config.py`` and
``orchestrator/routes/pipelines/__init__.py`` each carry a copy inside an
``except ImportError`` block, for deployments where ``shared/`` is not on
``sys.path``. #3630 re-synced the ``fix`` handling across all three;
without a test the copies can drift again silently, because a fallback
only executes when ``egg_config`` fails to import and is therefore never
exercised by an ordinary test run.

Reaching a fallback takes some care: in a configured checkout the ``try``
arm binds the canonical function and the ``def`` in the ``except`` arm is
never evaluated. Reimporting each module with ``egg_config`` blocked
would bind it, but drags in Flask blueprints, Docker clients and
orchestrator module state as a side effect. Instead each ``def`` is
lifted out of the source with ``ast`` and compiled on its own, in a
namespace holding the two globals it closes over (``Any`` and
``logger``). The test stays hermetic and still reads the real shipped
source, so a drifting copy fails here.

One caveat of loading from source text rather than importing: ``make
test``'s changeset narrowing sees no static import edge from those two
modules to this file, so editing them will not select this test locally.
``make test-all`` (CI ground truth) always runs it.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CANONICAL_SOURCE = "shared/egg_config/validators.py"

# Module path -> the ``except ImportError`` copy it carries.
FALLBACK_SOURCES = {
    "repo_config": "config/repo_config.py",
    "pipelines": "orchestrator/routes/pipelines/__init__.py",
}

# The extracted fallbacks log through whatever ``logger`` their host
# module defines; under exec we hand them one of our own so warnings are
# still emitted (and caplog still sees them, via propagation to root).
_FALLBACK_LOGGER = "tests.egg_config.validate_checks_parity"

ValidateChecks = Callable[[Any], list[dict[str, str]]]


def _parse(rel_path: str) -> ast.Module:
    return ast.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"), filename=rel_path)


def _handles_import_error(handler: ast.ExceptHandler) -> bool:
    """True for ``except ImportError``, ``except (ImportError, ...)``, and bare ``except``."""
    caught = handler.type
    if caught is None:
        return True
    names: list[ast.expr] = list(caught.elts) if isinstance(caught, ast.Tuple) else [caught]
    return any(isinstance(n, ast.Name) and n.id == "ImportError" for n in names)


def _find_fallback_def(rel_path: str) -> ast.FunctionDef:
    """Return the ``validate_checks`` def inside ``rel_path``'s ImportError handler."""
    for node in ast.walk(_parse(rel_path)):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if not _handles_import_error(handler):
                continue
            for stmt in handler.body:
                if isinstance(stmt, ast.FunctionDef) and stmt.name == "validate_checks":
                    return stmt
    raise AssertionError(
        f"{rel_path} has no validate_checks fallback in an `except ImportError` block. "
        "If the fallback was removed on purpose, drop its entry from FALLBACK_SOURCES."
    )


def _find_canonical_def() -> ast.FunctionDef:
    for node in _parse(CANONICAL_SOURCE).body:
        if isinstance(node, ast.FunctionDef) and node.name == "validate_checks":
            return node
    raise AssertionError(f"{CANONICAL_SOURCE} defines no module-level validate_checks")


def _compile_fallback(rel_path: str) -> ValidateChecks:
    """Compile ``rel_path``'s fallback def in isolation and return the function."""
    func_def = _find_fallback_def(rel_path)
    namespace: dict[str, Any] = {"Any": Any, "logger": logging.getLogger(_FALLBACK_LOGGER)}
    module = ast.Module(body=[func_def], type_ignores=[])
    exec(compile(module, rel_path, "exec"), namespace)
    return namespace["validate_checks"]


def _fix_block(func_def: ast.FunctionDef) -> ast.If:
    """Return the ``if "fix" in c:`` statement from a validate_checks def."""
    for node in ast.walk(func_def):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "'fix' in c":
            return node
    raise AssertionError('validate_checks has no `if "fix" in c:` block')


@pytest.fixture(params=["canonical", *FALLBACK_SOURCES])
def validate_checks(request) -> ValidateChecks:
    """Each of the three shipped copies of ``validate_checks``, in turn."""
    if request.param == "canonical":
        from egg_config.validators import validate_checks as canonical

        return canonical
    return _compile_fallback(FALLBACK_SOURCES[request.param])


class TestValidateChecksParity:
    """Every copy must agree on the whole normalization contract."""

    def test_valid_entry_retained(self, validate_checks):
        assert validate_checks([{"name": "lint", "command": "make lint"}]) == [
            {"name": "lint", "command": "make lint"}
        ]

    def test_non_list_input(self, validate_checks):
        assert validate_checks({"name": "lint"}) == []
        assert validate_checks(None) == []

    def test_malformed_entries_dropped(self, validate_checks):
        assert validate_checks(
            [
                {"name": "lint"},
                {"command": "make test"},
                "make lint",
                {"name": "ok", "command": "t"},
            ]
        ) == [{"name": "ok", "command": "t"}]

    def test_name_and_command_coerced_to_strings(self, validate_checks):
        assert validate_checks([{"name": 1, "command": 2}]) == [{"name": "1", "command": "2"}]

    def test_unknown_keys_dropped(self, validate_checks):
        assert validate_checks([{"name": "lint", "command": "make lint", "extra": "x"}]) == [
            {"name": "lint", "command": "make lint"}
        ]

    def test_valid_fix_retained(self, validate_checks):
        assert validate_checks(
            [{"name": "lint", "command": "make lint", "fix": "make lint-fix"}]
        ) == [{"name": "lint", "command": "make lint", "fix": "make lint-fix"}]

    # The #3630 rejection matrix: every value that is not a non-empty
    # string is dropped with a warning, in every copy.
    @pytest.mark.parametrize(
        "bad_fix",
        ["", "   ", "\n", None, False, 0, 3, ["make fmt", "make lint-fix"], {"cmd": "make fmt"}],
        ids=["empty", "spaces", "newline", "none", "false", "zero", "int", "list", "dict"],
    )
    def test_invalid_fix_dropped_with_warning(self, validate_checks, bad_fix, caplog):
        with caplog.at_level(logging.WARNING):
            result = validate_checks([{"name": "lint", "command": "make lint", "fix": bad_fix}])
        assert result == [{"name": "lint", "command": "make lint"}]
        assert "invalid fix" in caplog.text

    def test_absent_fix_does_not_warn(self, validate_checks, caplog):
        with caplog.at_level(logging.WARNING):
            result = validate_checks([{"name": "lint", "command": "make lint"}])
        assert result == [{"name": "lint", "command": "make lint"}]
        assert caplog.text == ""

    def test_full_command_retained(self, validate_checks):
        """All three copies carry ``full_command`` (#3669).

        The pipelines fallback omitted it until #3630 re-synced the
        copies, so a narrowed ``command`` could reach the propose-time
        check gate with no ground-truth form attached.
        """
        assert validate_checks(
            [{"name": "test", "command": "make test", "full_command": "make test-all"}]
        ) == [{"name": "test", "command": "make test", "full_command": "make test-all"}]

    def test_empty_full_command_dropped(self, validate_checks):
        assert validate_checks([{"name": "test", "command": "make test", "full_command": ""}]) == [
            {"name": "test", "command": "make test"}
        ]


class TestFixBlockIsIdentical:
    """The ``fix`` guard itself must stay byte-for-byte in sync.

    Behavioral parity above covers the cases we thought to enumerate;
    this catches drift in the parts a matrix cannot see — a reworded
    warning, a dropped ``%r`` argument, a guard rewritten in a way that
    happens to agree on all nine sampled values.
    """

    def test_all_three_copies_match(self):
        canonical = ast.dump(_fix_block(_find_canonical_def()))
        for name, rel_path in FALLBACK_SOURCES.items():
            fallback = ast.dump(_fix_block(_find_fallback_def(rel_path)))
            assert fallback == canonical, (
                f"the validate_checks `fix` guard in {rel_path} ({name}) has drifted from "
                f"{CANONICAL_SOURCE}; re-sync the block or update this test deliberately"
            )
