"""Shared overseer helpers (issue #1962).

This package consolidates the overseer-specific helpers that need to be
imported from both the orchestrator and the sandbox without crossing the
package boundary. It hosts the advisor wrapper, the secret-scrubber, the
infra-error classifier, the priority-dimension mapping, the on-disk state
schemas + helpers, and the canonical issue-body template.

Exports are re-exported lazily by submodule; callers import from the
specific submodule they need (e.g.
``from egg_overseer.advisor import consult_advisor``).

Note on naming: the planner referred to this package as
``shared/overseer/`` in the plan document. The actual Python package name
is ``egg_overseer`` to match the existing ``egg_*`` convention used by
``egg_orchestrator``, ``egg_health``, etc., because ``shared/`` itself is
on ``PYTHONPATH`` at runtime (``/opt/egg-runtime/shared``) and the
top-level ``shared`` namespace is not importable in the sandbox.
"""
