"""Two-way rule-doc drift gate + decision-13 docstring-rationale gate
(#1917, iter-2, TASK-5-2).

Three assertions:

A. Every ``Prefer this over `egg-*``` line in the agent rule docs
   (``sandbox/agent-config/rules/*.md`` and
   ``sandbox/egg_lib/data/hitl_editing_rules.md``) points at a tool
   whose CLI counterpart matches the advertised shell command.
B. Every registration in ``TOOL_REGISTRY`` with
   ``cli_command != None`` has a matching rule-doc entry in at least
   one of those docs (so adding a CLI-backed tool forces a rule-doc
   update).
C. Every registration with ``cli_command == None`` resolves to a
   handler whose ``__doc__`` is non-empty AND contains the substring
   ``"no CLI"`` or ``"no-CLI"`` (decision-13 closes the gap that was
   previously untested).

The regex is pinned to the iter-1 phrasing
(``Prefer this over `egg-…```); if a future iteration wants to
introduce a different idiom, update the pattern here alongside the
rule-doc sweep.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.tools import TOOL_REGISTRY  # noqa: E402

# Agent-rule-doc files + the HITL editor rules.  Kept as a sorted tuple
# so regression diffs are deterministic when new rule docs land.
_RULE_DOC_GLOBS: tuple[Path, ...] = tuple(
    sorted(
        [
            *(ROOT / "sandbox" / "agent-config" / "rules").glob("*.md"),
            ROOT / "sandbox" / "egg_lib" / "data" / "hitl_editing_rules.md",
        ]
    )
)

# Regex for a "Prefer this over …" line naming a specific MCP tool.
# Example matched line:
#   - `mcp__sdlc__show_contract` — Prefer this over `egg-contract show`.
# Groups:
#   1 = mcp tool name
#   2 = shell command (between the second pair of backticks, stripped
#       of surrounding punctuation).
_PREFER_RE = re.compile(
    r"`(mcp__[a-z][a-z0-9_]*__[a-z][a-z0-9_]*)`[^\n]*?"
    r"Prefer this over `(egg-[a-z][a-z0-9_ -]*?)`",
    re.IGNORECASE,
)


# --------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------


def _rule_doc_entries() -> list[tuple[Path, str, str]]:
    """Return ``(doc_path, tool_name, cli_command_str)`` for every
    Prefer-this-over line across the rule docs."""
    entries: list[tuple[Path, str, str]] = []
    for path in _RULE_DOC_GLOBS:
        text = path.read_text()
        for m in _PREFER_RE.finditer(text):
            entries.append((path, m.group(1), m.group(2).strip()))
    return entries


def _cli_command_str(cli: tuple[str, ...]) -> str:
    """Return the rule-doc-style shell command string for a tuple."""
    return " ".join(cli)


# --------------------------------------------------------------------
# A. Every Prefer line resolves to a registered tool + CLI matches
# --------------------------------------------------------------------


class TestRuleDocToRegistry:
    def test_at_least_one_prefer_line_found(self):
        """Sanity: the regex must match SOMETHING — catches a regression
        where the Prefer-this-over phrasing is renamed without updating
        the regex here."""
        entries = _rule_doc_entries()
        assert entries, (
            "No 'Prefer this over `egg-…`' lines matched in any rule "
            "doc.  Either the phrasing changed (update the regex here) "
            "or every rule-doc line was accidentally removed."
        )

    @pytest.mark.parametrize(
        "path,tool_name,cli_str",
        _rule_doc_entries(),
        ids=lambda p: p if isinstance(p, str) else p.name,
    )
    def test_every_prefer_line_names_registered_tool(
        self, path: Path, tool_name: str, cli_str: str
    ):
        """Assertion A: a Prefer-this-over line must name a tool in
        ``TOOL_REGISTRY`` AND the shell command must match the tool's
        declared ``cli_command``."""
        assert tool_name in TOOL_REGISTRY, (
            f"{path.name}: 'Prefer this over' names {tool_name!r} but "
            f"no such entry in TOOL_REGISTRY"
        )
        reg = TOOL_REGISTRY[tool_name]
        assert reg.cli_command is not None, (
            f"{path.name}: 'Prefer this over' names {tool_name!r} which "
            f"has cli_command=None — rule docs should not advertise a "
            f"CLI replacement for no-CLI tools"
        )
        expected = _cli_command_str(reg.cli_command)
        # Be lenient about whitespace: the rule doc may include or omit
        # trailing hyphen/space artefacts.
        assert cli_str.strip() == expected, (
            f"{path.name}: tool {tool_name!r} rule-doc CLI '{cli_str}' "
            f"disagrees with registered CLI '{expected}'"
        )


# --------------------------------------------------------------------
# B. Every cli_command != None tool has a rule-doc entry
# --------------------------------------------------------------------


class TestRegistryToRuleDoc:
    """Flip of assertion A: every registered CLI-backed tool must have
    a rule-doc entry somewhere.  Adding a new CLI-backed tool that
    forgets the rule-doc sweep trips this test immediately."""

    def _documented_tools(self) -> set[str]:
        return {tool for _, tool, _ in _rule_doc_entries()}

    def test_every_cli_backed_tool_has_rule_doc_entry(self):
        documented = self._documented_tools()
        missing: list[str] = []
        for name, reg in TOOL_REGISTRY.items():
            if reg.cli_command is not None and name not in documented:
                missing.append(name)
        assert not missing, (
            "The following CLI-backed tools have no rule-doc "
            "'Prefer this over …' entry; add one to "
            "sandbox/agent-config/rules/*.md or "
            "sandbox/egg_lib/data/hitl_editing_rules.md:\n"
            + "\n".join(f"  - {n}" for n in sorted(missing))
        )


# --------------------------------------------------------------------
# C. Every cli_command == None handler docstring mentions "no CLI"
# --------------------------------------------------------------------


class TestNoCliDocstringRationale:
    """Assertion C (decision-13): every ``cli_command=None`` tool's
    handler docstring must be non-empty AND contain ``"no CLI"`` or
    ``"no-CLI"`` (case-insensitive).  This closes the decision-13 gap
    that was previously untested."""

    def test_every_no_cli_handler_has_rationale(self):
        failures: list[str] = []
        for name, reg in TOOL_REGISTRY.items():
            if reg.cli_command is not None:
                continue
            doc = reg.handler.__doc__ or ""
            if not doc.strip():
                failures.append(f"{name}: empty handler docstring")
                continue
            lower = doc.lower()
            if "no cli" not in lower and "no-cli" not in lower:
                failures.append(
                    f"{name}: handler docstring lacks a 'no CLI' rationale (decision-13)"
                )
        assert not failures, "\n".join(failures)


# --------------------------------------------------------------------
# Guard-rail tests: the plan promises three very specific failure modes
# --------------------------------------------------------------------


class TestGuardRailScenarios:
    """Pin the three failure modes called out in the plan so future
    refactors don't silently disable these assertions.

    These tests patch the registry in-memory to simulate the failure,
    then invoke the underlying helpers directly.  They never touch the
    on-disk rule docs.
    """

    def test_spurious_prefer_line_for_unregistered_tool_trips_assertion_a(self):
        """If a rule-doc line names a tool that's NOT in TOOL_REGISTRY,
        assertion A must fail."""
        fake = ("/tmp/fake.md", "mcp__sdlc__nonexistent", "egg-contract fake")
        # Direct invocation to skip pytest's parametrize dispatcher.
        with pytest.raises(AssertionError):
            TestRuleDocToRegistry().test_every_prefer_line_names_registered_tool(
                Path(fake[0]), fake[1], fake[2]
            )

    def test_cli_backed_tool_with_no_rule_doc_entry_trips_assertion_b(self, monkeypatch):
        """If TOOL_REGISTRY gains a new CLI-backed tool and no rule-doc
        entry lands, assertion B must fail."""
        from egg_agent_tools.tools._registry import ToolRegistration

        fake_handler = lambda req: req  # noqa: E731
        fake_tool = ToolRegistration(
            name="mcp__sdlc__fake_new_verb",
            namespace="sdlc",
            handler=fake_handler,
            sdk_tool=object(),
            cli_command=("egg-contract", "fake-new-verb"),
        )
        patched_registry = {**TOOL_REGISTRY, fake_tool.name: fake_tool}
        monkeypatch.setattr(
            "tests.tools.test_rule_doc_drift.TOOL_REGISTRY",
            patched_registry,
        )
        with pytest.raises(AssertionError) as exc:
            TestRegistryToRuleDoc().test_every_cli_backed_tool_has_rule_doc_entry()
        assert "mcp__sdlc__fake_new_verb" in str(exc.value)

    def test_no_cli_handler_with_empty_docstring_trips_assertion_c(self, monkeypatch):
        """If a cli_command=None handler has an empty or rationale-less
        docstring, assertion C must fail."""
        from egg_agent_tools.tools._registry import ToolRegistration

        def no_doc_handler(req):
            return req

        # Explicitly blank the docstring.
        no_doc_handler.__doc__ = ""
        fake_tool = ToolRegistration(
            name="mcp__sdlc__fake_no_cli_verb",
            namespace="sdlc",
            handler=no_doc_handler,
            sdk_tool=object(),
            cli_command=None,
        )
        patched_registry = {**TOOL_REGISTRY, fake_tool.name: fake_tool}
        monkeypatch.setattr(
            "tests.tools.test_rule_doc_drift.TOOL_REGISTRY",
            patched_registry,
        )
        with pytest.raises(AssertionError) as exc:
            TestNoCliDocstringRationale().test_every_no_cli_handler_has_rationale()
        assert "mcp__sdlc__fake_no_cli_verb" in str(exc.value)
