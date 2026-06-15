"""Guard that reviewer-facing wrapper CLIs are reachable on the sandbox PATH.

The event-pump's first-review fallback hands reviewers a bare ``egg-artifact
get <name> --ref <sha>`` command (orchestrator/routes/event_prompt.py). The
reviewer runs that through the Bash tool, so ``egg-artifact`` must resolve as a
bare command in the sandbox image. It ships as ``sandbox/scripts/egg-artifact``,
but ``sandbox/scripts/`` is *not* on PATH — only ``sandbox/bin`` is (the
Dockerfile's image-level ``ENV PATH``). The two surfaces drifted in #3221: the
renderer depended on ``egg-artifact`` before anything installed it on PATH, so a
reviewer following the prompt got ``command not found`` while every unit test
(which only assert the rendered *string*) stayed green.

These tests pin the install surface to the rendered command so they cannot
silently drift apart again.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX = _REPO_ROOT / "sandbox"
_DOCKERFILE = _SANDBOX / "Dockerfile"

# Directory the Dockerfile prepends to PATH at the image level (issue #1799).
_PATH_DIR_ON_IMAGE = "/opt/egg-runtime/sandbox/bin"

# Bare wrapper commands the orchestrator renders into reviewer prompts. Each
# must be resolvable on PATH, i.e. present in sandbox/bin and pointing at an
# existing executable.
#
# MAINTENANCE: this set is coupled to the renderer (event_prompt.py) only via
# the shared command string — there is no derivation, so if the renderer starts
# emitting a *different* bare command this guard stays green while silently
# diverging. When you add a bare command to the reviewer fallback prompt, add it
# here too (and pin its rendered string in test_compose_event_prompt.py).
_REVIEWER_BARE_COMMANDS = ("egg-artifact",)


def test_dockerfile_puts_sandbox_bin_on_path() -> None:
    """The PATH dir these wrappers live in must actually be on PATH."""
    dockerfile = _DOCKERFILE.read_text()
    assert _PATH_DIR_ON_IMAGE in dockerfile, (
        f"Dockerfile no longer puts {_PATH_DIR_ON_IMAGE} on PATH; update this guard."
    )


def test_reviewer_wrappers_are_installed_on_path() -> None:
    """Each bare command rendered into reviewer prompts resolves on PATH."""
    bin_dir = _SANDBOX / "bin"
    for cmd in _REVIEWER_BARE_COMMANDS:
        wrapper = bin_dir / cmd
        assert wrapper.is_symlink() or wrapper.is_file(), (
            f"{cmd!r} is rendered as a bare command in reviewer prompts but is "
            f"not installed in sandbox/bin (which is on PATH). Add it so the "
            f"rendered command resolves."
        )
        # Resolve through symlinks and confirm the target exists and is runnable.
        target = wrapper.resolve()
        assert target.is_file(), f"{cmd!r} -> {target} does not resolve to a file."
        assert os.access(target, os.X_OK), f"{cmd!r} -> {target} is not executable."
