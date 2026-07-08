"""Overseer primary-repo resolution for multi-repo pipelines (#3393 slice-3).

``routes.pipelines._spawn_overseer_agent`` resolves the overseer's model from
the pipeline's PRIMARY (first) repo. Before slice-3 the multi-repo list was
collapsed with a positional ``repos[0]``; slice-3 replaced that with
``next(iter(pipeline_repos or []), None)`` so the collapse token is gone. The
``TestReposZeroCollapseRatchet`` ratchet proves the *token* is absent; these
tests prove the *resolution is correct* — the primary entry is passed for a
2-repo pipeline, and ``None`` (never ``IndexError``) for an empty/absent list.

This lived in its own module rather than ``test_phase_scoped_overseer.py``
deliberately: that module gated its whole import on
``routes.pipelines._check_and_respawn_overseer``, a symbol deleted by the
#2270 slice-5 fold, so the module was skipped repo-wide and any assertion
added there would *skip*, not execute (it has since been removed entirely,
#3513). The resolution site under test — ``_spawn_overseer_agent`` — imports
cleanly, so this module gates only on that and actually runs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# conftest already inserts orchestrator/ + shared/ on sys.path; explicit is
# fine for test-discovery tooling that imports this file directly.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that (transitively) depend on it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

try:
    from routes.pipelines import _spawn_overseer_agent
except ImportError as exc:  # pragma: no cover - environment guard
    pytest.skip(
        f"Required orchestrator modules not available: {exc}",
        allow_module_level=True,
    )


def _patch_model_resolution(monkeypatch) -> dict:
    """Patch ``resolve_overseer_model`` / ``build_agent_command`` and return a
    dict capturing the ``repo`` the overseer model was resolved from.

    Both symbols are late-imported *inside* ``_spawn_overseer_agent`` (``from
    agent_model_resolution import ...`` / ``from egg_agent import ...``), so
    patching them on their defining modules intercepts the call at runtime.
    """
    import agent_model_resolution as amr
    import egg_agent
    from agent_model_resolution import DEFAULT_AGENT_MODEL, classify_model

    captured: dict[str, object] = {}

    def fake_resolve(*_args, repo=None, **_kwargs):
        captured["repo"] = repo
        # Return a real decision object so the downstream env/model plumbing
        # (``.env_vars()``, ``.claude_code_alias``, ``.upstream`` …) works.
        return classify_model(DEFAULT_AGENT_MODEL)

    monkeypatch.setattr(amr, "resolve_overseer_model", fake_resolve)
    monkeypatch.setattr(egg_agent, "build_agent_command", lambda **_kw: ["overseer-cmd"])
    return captured


class TestOverseerRepoResolution:
    def test_overseer_repo_resolves_to_primary_for_two_repo_pipeline(self, monkeypatch):
        """A 2-repo pipeline resolves the overseer model from the FIRST
        (primary) repo — not the second, and not a collapsed positional pick."""
        captured = _patch_model_resolution(monkeypatch)
        spawner = MagicMock()

        _spawn_overseer_agent(
            spawner=spawner,
            pipeline_id="issue-3393",
            issue_number=3393,
            gateway_mode="public",
            pipeline_repos=["ownerA/schema", "ownerB/consumer"],
            max_turns=5,
        )

        assert captured["repo"] == "ownerA/schema"
        spawner.spawn_agent_job.assert_called_once()

    def test_overseer_repo_is_none_for_empty_or_absent_pipeline_repos(self, monkeypatch):
        """An empty list and ``None`` both resolve ``overseer_repo`` to ``None``
        (the ``next(iter(..., None))`` default) — never ``IndexError``."""
        captured = _patch_model_resolution(monkeypatch)

        for repos in ([], None):
            captured.clear()
            _spawn_overseer_agent(
                spawner=MagicMock(),
                pipeline_id="issue-3393",
                issue_number=3393,
                gateway_mode="public",
                pipeline_repos=repos,
                max_turns=5,
            )
            assert captured["repo"] is None
