#!/usr/bin/env python3
"""Pre-flight check for the egg-sdlc skill (#2623, TASK-1-7).

The skill's outer loop runs this before booting the in-process
orchestrator. Verifies that the egg Python packages are importable.
On failure, emits a clear install instruction matching the one
documented in the skill's SKILL.md (TASK-1-11 alignment).

Exits 0 on success; exits 1 with a human-readable install
instruction on failure. Designed to be called from a Bash skill
step, e.g.::

    python3 "$SKILL_ROOT/bin/preflight.py" || exit 1
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Marker substring tests use to detect the install-error path.
INSTALL_ERROR_MARKER = "egg-sdlc: required Python packages not importable"


def _load_python_dep_string() -> str:
    """Read the pip dependency string from plugin.json (cq-12).

    Returns the literal value the operator selects via cq-12. When
    the decision is unresolved, the plugin metadata carries a TODO
    placeholder pointing at the follow-up issue; the pre-flight
    helper surfaces the same TODO so the user has actionable
    information.
    """
    plugin_json_path = (
        Path(__file__).resolve().parent.parent.parent.parent / ".claude-plugin" / "plugin.json"
    )
    try:
        data = json.loads(plugin_json_path.read_text())
    except OSError, json.JSONDecodeError:
        return ""
    egg = data.get("egg") or {}
    return str(egg.get("python_dependency") or "")


def main() -> int:
    """Probe the actual import path the skill's runtime uses.

    Earlier versions (reviewer_code_holistic v1 blocker #7) imported
    ``egg_orchestrator`` — that package is the orchestrator API
    CLIENT, not the substrate orchestrator entry point. The skill's
    actual runtime dependency is
    ``orchestrator.substrate.in_process.run_pipeline_in_process``,
    so we probe that exact import. A user who installs the API
    client but not the orchestrator code now sees the install error
    instead of passing preflight and then crashing later.
    """
    missing: str | None = None
    try:
        from orchestrator.substrate.in_process import (  # noqa: F401
            run_pipeline_in_process,
        )
    except ImportError as exc:
        missing = f"orchestrator.substrate.in_process.run_pipeline_in_process: {exc}"

    if missing is not None:
        dep = _load_python_dep_string()
        print(INSTALL_ERROR_MARKER, file=sys.stderr)
        print(
            "  ImportError: orchestrator.substrate.in_process is not\n"
            "  importable. The skill needs the egg orchestrator code on\n"
            "  PYTHONPATH (or installed as a package).\n"
            "\n"
            "  Install the egg Python packages, then retry:\n",
            file=sys.stderr,
        )
        if dep:
            # Match the SKILL.md install snippet structure verbatim.
            print(f"    pip install {dep}\n", file=sys.stderr)
        else:
            print(
                "    pip install <pip-name-from-plugin.json>  # see "
                "plugins/egg-sdlc/.claude-plugin/plugin.json\n",
                file=sys.stderr,
            )
        print(
            "  See plugins/egg-sdlc/skills/egg-sdlc/SKILL.md for the\n"
            "  full install instructions (cq-12).\n"
            "\n"
            f"  Underlying import error: {missing}\n",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
