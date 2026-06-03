"""egg_agent_tools — pure handlers + the gateway push helper.

This package historically exposed egg's agent-lifecycle capabilities as
first-class MCP tools (HITL decisions, BRC consensus, phase context,
progress signalling, task completion).  The in-process MCP tool surface
was retired in #2908 slice-6 in favour of the ``egg-orch`` /
``egg-contract`` shell CLIs (``sandbox/egg_lib/orch_cli.py`` and
``sandbox/egg_lib/contract_cli.py``).  The SDK ``@tool`` wrappers, the
server factory, and the system-prompt nudge constant are gone.

Layout
------

- ``handlers/`` — pure request→response functions (dict in, dict out).
  The shell CLIs (``sandbox/egg_lib/contract_cli.py`` and
  ``sandbox/egg_lib/orch_cli.py``) call these directly; any behaviour
  change lands in one place.
- ``push.py`` — gateway push helper invoked by the consensus-propose
  CLI path (issue #1994).

Gating
------

The env-flag check that previously gated the in-process MCP surface
was removed alongside the MCP registration block in
``shared/egg_agent/client.py`` (#2908 slice-6, task-6-2).  Sandbox
agents now reach the same capabilities through the shell CLIs; the
operator-facing MCP server at ``orchestrator/mcp_server.py`` is
unaffected.
"""

from __future__ import annotations

__all__: list[str] = []
