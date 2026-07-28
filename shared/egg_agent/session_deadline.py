"""Make the one-shot session's wall-clock budget visible to the agent (#3658).

Every one-shot agent runs under a hard execution budget (``run_agent(timeout=)``,
default 7200s). Before this module nothing told the agent that budget existed:
not the event prompt, not the environment, not any tool. An agent that knows it
has ten minutes left commits; one that does not, does not — so the #3639
mitigation ("commit early, leave the tree clean") was unfollowable in principle.

Two surfaces, both cheap and both derived from the same two numbers:

  * :func:`render_deadline_banner` — a short prompt section stating the budget,
    the start time, and the absolute UTC deadline. Absolute, not just
    "N seconds remaining": a relative figure is stale the moment the agent reads
    it, whereas a UTC instant stays checkable all session with ``date -u``.
  * :func:`export_deadline_env` — the same instant in the environment
    (``EGG_SESSION_DEADLINE_EPOCH`` / ``EGG_SESSION_BUDGET_SECONDS``), inherited
    by every tool call and hook the agent spawns, so a script can compute the
    remaining time without parsing prose.

The banner is **appended**, not prepended, and that is load-bearing rather than
stylistic. Its timestamps vary per invocation, so at the front of the prompt it
would sit ahead of the byte-identical shared-evidence prefix a reviewer wave
relies on for its prompt-cache hit (``evidence_gatherer``, #3523 S7) and destroy
it. As a suffix it cannot invalidate any prefix, and the deadline lands in the
recency position where an operational constraint reads best anyway.

``EGG_SESSION_DEADLINE_BANNER=false`` disables the prompt section (the env
export is inert and stays); the rollback restores a byte-identical prompt.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

# Env var carrying the session deadline as a Unix epoch (seconds, integer) and
# the budget it was derived from. Read by anything in-pod that wants the clock:
# tools, hooks, the agent itself via ``printenv``.
DEADLINE_EPOCH_ENV = "EGG_SESSION_DEADLINE_EPOCH"
BUDGET_SECONDS_ENV = "EGG_SESSION_BUDGET_SECONDS"

# Rollback hatch for the prompt preamble only.
BANNER_ENV = "EGG_SESSION_DEADLINE_BANNER"


def is_banner_disabled() -> bool:
    """Return True iff the prompt preamble is switched off.

    Default OFF-switch semantics (the banner is ON unless explicitly disabled):
    only the explicit falsey spellings disable it, so an unset or garbled value
    keeps the deadline visible — the failure mode of a typo should be an agent
    that can see its clock, not one that silently cannot.
    """
    return os.environ.get(BANNER_ENV, "").strip().lower() in {"0", "false", "no", "off"}


def _format_budget(seconds: int) -> str:
    """Render a seconds budget as ``2h 0m`` / ``45m`` for the banner."""
    if seconds >= 3600:
        hours, remainder = divmod(seconds, 3600)
        return f"{hours}h {remainder // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def export_deadline_env(timeout_seconds: int, started_at: float) -> None:
    """Publish the session deadline into the process environment.

    Inherited by every subprocess the agent spawns (tool calls, hooks, the
    ``egg-orch`` CLI), so the budget is reachable without parsing the prompt.
    Non-positive budgets export nothing — there is no deadline to describe.
    """
    if timeout_seconds <= 0:
        return
    os.environ[BUDGET_SECONDS_ENV] = str(int(timeout_seconds))
    os.environ[DEADLINE_EPOCH_ENV] = str(int(started_at + timeout_seconds))


def render_deadline_banner(timeout_seconds: int, started_at: float) -> str:
    """Return the deadline section to APPEND to the session prompt.

    Empty string when the banner is disabled or the budget is non-positive, in
    which case the caller's prompt is passed through byte-identically. See the
    module docstring for why this is a suffix and not a preamble.
    """
    if timeout_seconds <= 0 or is_banner_disabled():
        return ""
    start = datetime.fromtimestamp(started_at, UTC)
    deadline = datetime.fromtimestamp(started_at + timeout_seconds, UTC)
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return (
        "\n\n---\n\n"
        "## Session budget (hard deadline)\n"
        "\n"
        f"This session has a hard wall-clock budget of {timeout_seconds}s "
        f"({_format_budget(timeout_seconds)}). It started at "
        f"{start.strftime(stamp)} and is killed at **{deadline.strftime(stamp)}** "
        "with no further warning.\n"
        "\n"
        "Check the remaining time at any point with `date -u` and compare it "
        "against that deadline. Commit your work *before* it: the next "
        "invocation re-attaches to this same worktree and continues, so a "
        "session boundary you committed for costs you nothing, while an "
        "in-flight edit at the kill costs you the reasoning behind it.\n"
        "\n"
        "If you are close to the deadline, stop starting new work: commit what "
        "you have, record where you got to in durable BRC memory, and exit.\n"
    )
