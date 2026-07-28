"""The session's wall-clock budget is visible to the agent (#3658).

Before this, nothing told a one-shot agent how long it had: not the prompt, not
the environment, not any tool. It was killed at 7200s with no warning, which
made the #3639 mitigation ("commit early, leave the tree clean") unfollowable —
you cannot commit before a deadline you cannot see.

These tests pin the two surfaces that fix that, and the invariant that matters
more than either: a disabled banner leaves the prompt byte-identical.
"""

from __future__ import annotations

from egg_agent.session_deadline import (
    BANNER_ENV,
    BUDGET_SECONDS_ENV,
    DEADLINE_EPOCH_ENV,
    export_deadline_env,
    is_banner_disabled,
    render_deadline_banner,
)

# 2026-07-27T01:19:58Z — the #3639 incident's coder-spawn instant, so the
# rendered deadline below is the 03:19:58Z the pipeline actually died at.
_START = 1785115198.0


def test_banner_states_the_budget_and_the_absolute_deadline():
    banner = render_deadline_banner(7200, _START)
    # The budget, so the agent can reason about how much it started with...
    assert "7200s" in banner
    assert "2h 0m" in banner
    # ...and the absolute instant, which stays checkable all session (a
    # "remaining" figure would be stale the moment it is read).
    assert "2026-07-27T03:19:58Z" in banner
    # The recipe for checking it mid-session.
    assert "date -u" in banner


def test_banner_is_a_suffix_so_it_cannot_break_a_cacheable_prefix():
    """Load-bearing, not stylistic (see the module docstring).

    The banner's timestamps vary per invocation. At the FRONT of the prompt it
    would sit ahead of the byte-identical shared-evidence prefix a reviewer wave
    shares for its prompt-cache hit, and destroy it. Appended, it cannot
    invalidate any prefix.
    """
    banner = render_deadline_banner(7200, _START)
    prompt = "SHARED CACHEABLE EVIDENCE PREFIX" + "\nper-event body"

    assert (prompt + banner).startswith(prompt)
    assert banner.startswith("\n")


def test_banner_disabled_yields_a_byte_identical_prompt(monkeypatch):
    monkeypatch.setenv(BANNER_ENV, "false")
    assert render_deadline_banner(7200, _START) == ""


def test_banner_only_disabled_by_explicit_falsey_spellings(monkeypatch):
    for value in ("0", "false", "FALSE", "no", "off"):
        monkeypatch.setenv(BANNER_ENV, value)
        assert is_banner_disabled(), value
    # A typo must leave the deadline VISIBLE — the failure mode of a garbled
    # value should not be a blind agent.
    for value in ("", "1", "true", "yes", "falsee", "  "):
        monkeypatch.setenv(BANNER_ENV, value)
        assert not is_banner_disabled(), value


def test_no_banner_without_a_budget():
    assert render_deadline_banner(0, _START) == ""
    assert render_deadline_banner(-1, _START) == ""


def test_budget_formatting_spans_the_plausible_range():
    assert "2h 0m" in render_deadline_banner(7200, _START)
    assert "1h 30m" in render_deadline_banner(5400, _START)
    assert "45m" in render_deadline_banner(2700, _START)
    assert "30s" in render_deadline_banner(30, _START)


def test_env_export_carries_the_deadline_to_subprocesses(monkeypatch):
    monkeypatch.delenv(DEADLINE_EPOCH_ENV, raising=False)
    monkeypatch.delenv(BUDGET_SECONDS_ENV, raising=False)
    export_deadline_env(7200, _START)
    import os

    assert os.environ[BUDGET_SECONDS_ENV] == "7200"
    assert os.environ[DEADLINE_EPOCH_ENV] == str(int(_START + 7200))


def test_env_export_is_inert_without_a_budget(monkeypatch):
    monkeypatch.delenv(DEADLINE_EPOCH_ENV, raising=False)
    monkeypatch.delenv(BUDGET_SECONDS_ENV, raising=False)
    export_deadline_env(0, _START)
    import os

    assert DEADLINE_EPOCH_ENV not in os.environ
    assert BUDGET_SECONDS_ENV not in os.environ


def test_env_export_survives_the_banner_being_disabled(monkeypatch):
    """The rollback hatch is for the PROMPT only — tooling keeps the clock."""
    monkeypatch.setenv(BANNER_ENV, "false")
    monkeypatch.delenv(DEADLINE_EPOCH_ENV, raising=False)
    export_deadline_env(7200, _START)
    import os

    assert os.environ[DEADLINE_EPOCH_ENV] == str(int(_START + 7200))


# ---------------------------------------------------------------------------
# Continuation promises are event-pump properties, not CLI properties
# ---------------------------------------------------------------------------
#
# "The next invocation re-attaches to this same worktree" and "record where you
# got to in durable BRC memory" are true of the orchestrator's event pump. They
# are not true of ``python3 -m egg_agent``, which is a general CLI: asserting
# them to a standalone run points the agent at a memory file that does not exist
# and promises a continuation that will not happen. Both are therefore gated on
# the pipeline identity the orchestrator exports for every pumped pod.


def _as_event_pump(monkeypatch):
    monkeypatch.setenv("EGG_PIPELINE_ID", "p-1234")
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")


def _as_standalone(monkeypatch):
    monkeypatch.delenv("EGG_PIPELINE_ID", raising=False)
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)


def test_event_pump_banner_promises_the_continuation(monkeypatch):
    _as_event_pump(monkeypatch)

    banner = render_deadline_banner(7200, _START)

    assert "re-attaches to this same worktree" in banner
    assert "durable BRC memory" in banner


def test_standalone_banner_promises_nothing_it_cannot_keep(monkeypatch):
    _as_standalone(monkeypatch)

    banner = render_deadline_banner(7200, _START)

    assert "re-attaches" not in banner
    assert "BRC memory" not in banner
    # The advice that IS true of any invocation must survive the gate — a
    # standalone agent still benefits from committing before the kill.
    assert "commit" in banner.lower()
    assert "2026-07-27T03:19:58Z" in banner


def test_a_half_set_identity_is_treated_as_standalone(monkeypatch):
    """Both halves or neither.

    The memory path is built from the pipeline id *and* the role, so one without
    the other names a file that cannot exist. Empty-but-set counts as unset for
    the same reason — the orchestrator exports the variable either way.
    """
    for pipeline_id, role in (("p-1234", ""), ("", "coder"), ("  ", "coder")):
        monkeypatch.setenv("EGG_PIPELINE_ID", pipeline_id)
        monkeypatch.setenv("EGG_AGENT_ROLE", role)
        assert "BRC memory" not in render_deadline_banner(7200, _START), (pipeline_id, role)
