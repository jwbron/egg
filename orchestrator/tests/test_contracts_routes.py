"""Tests for orchestrator contract endpoints (#1781).

These exercise the single source of truth for contract state:
the orchestrator's ``/api/v1/contracts/…`` endpoints backed by the
shared pipeline worktree.  Gateway interactions are tested
separately in ``gateway/tests/test_contract_api.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import contract_store
import pytest
from api import app
from egg_contracts import Contract, create_contract


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def fake_worktree(tmp_path: Path, monkeypatch):
    """Create a fake pipeline worktree that contract_store can discover."""
    pipeline_id = "issue-1781"
    worktrees_base = tmp_path / "worktrees"
    worktrees_base.mkdir()

    worktree = worktrees_base / pipeline_id / "egg"
    worktree.mkdir(parents=True)
    (worktree / ".git").mkdir()

    monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", worktrees_base)

    return pipeline_id, worktree


def _seed_contract(worktree: Path, pipeline_id: str):
    create_contract(pipeline_id=pipeline_id, title="Fixture", repo_root=worktree)


class TestGetContract:
    def test_returns_contract(self, client, fake_worktree):
        pipeline_id, worktree = fake_worktree
        _seed_contract(worktree, pipeline_id)

        response = client.get(
            f"/api/v1/contracts/{pipeline_id}",
            query_string={"pipeline_id": pipeline_id, "repo": "egg"},
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["data"]["pipeline_id"] == pipeline_id
        assert data["source"] == "worktree"

    def test_missing_pipeline_id_rejected(self, client, fake_worktree):
        pipeline_id, _ = fake_worktree
        response = client.get(f"/api/v1/contracts/{pipeline_id}")
        assert response.status_code == 400

    def test_worktree_not_found(self, client, fake_worktree):
        response = client.get(
            "/api/v1/contracts/some-id",
            query_string={"pipeline_id": "missing-id"},
        )
        assert response.status_code == 404

    def test_contract_not_found(self, client, fake_worktree):
        pipeline_id, _ = fake_worktree
        response = client.get(
            f"/api/v1/contracts/{pipeline_id}",
            query_string={"pipeline_id": pipeline_id, "repo": "egg"},
        )
        assert response.status_code == 404

    def test_invalid_identifier_rejected(self, client, fake_worktree):
        pipeline_id, _ = fake_worktree
        response = client.get(
            "/api/v1/contracts/has space",
            query_string={"pipeline_id": pipeline_id, "repo": "egg"},
        )
        assert response.status_code == 400


class TestMutateContract:
    def _mutate(self, client, identifier, role, body_overrides=None):
        body = {
            "pipeline_id": identifier,
            "repo": "egg",
            "field_path": "phases",
            "new_value": [],
        }
        if body_overrides:
            body.update(body_overrides)
        return client.post(
            f"/api/v1/contracts/{identifier}/mutate",
            json=body,
            headers={"X-Egg-Role": role},
        )

    def test_rejects_when_role_missing(self, client, fake_worktree):
        pipeline_id, worktree = fake_worktree
        _seed_contract(worktree, pipeline_id)

        response = client.post(
            f"/api/v1/contracts/{pipeline_id}/mutate",
            json={
                "pipeline_id": pipeline_id,
                "repo": "egg",
                "field_path": "phases",
                "new_value": [],
            },
        )
        assert response.status_code == 403
        assert "role" in json.loads(response.data)["message"].lower()

    def test_writes_visible_from_second_call(self, client, fake_worktree):
        """Core regression for #1781: a mutation on one request is
        immediately visible to the next.  Under the pre-fix
        per-agent-worktree design, producer and consumer saw
        divergent state.
        """
        pipeline_id, worktree = fake_worktree
        _seed_contract(worktree, pipeline_id)

        decision = {
            "id": "decision-1",
            "question": "Is this mutation visible?",
            "type": "hitl",
            "options": [],
            "resolved": False,
        }

        producer = self._mutate(
            client,
            pipeline_id,
            role="implementer",
            body_overrides={"field_path": "decisions.0", "new_value": decision},
        )
        assert producer.status_code == 200, producer.data

        consumer = client.get(
            f"/api/v1/contracts/{pipeline_id}",
            query_string={"pipeline_id": pipeline_id, "repo": "egg"},
        )
        assert consumer.status_code == 200
        decisions = json.loads(consumer.data)["data"]["decisions"]
        assert any(d["id"] == "decision-1" for d in decisions)

    def test_role_validation_enforced(self, client, fake_worktree):
        pipeline_id, worktree = fake_worktree
        _seed_contract(worktree, pipeline_id)

        # Reviewer role cannot register a task commit — that's
        # implementer territory.
        response = self._mutate(
            client,
            pipeline_id,
            role="reviewer",
            body_overrides={
                "field_path": "phases.0.tasks.0.commit",
                "new_value": "abc1234",
            },
        )
        assert response.status_code == 403

    def test_invalid_path_returns_400(self, client, fake_worktree):
        """#2495: out-of-range index is a value error, not authorization.

        Implementer is authorized to mutate ``phases.*.status``, but the
        seeded contract has no phases — index 0 is out of range and
        ``apply_mutation`` translates the ``IndexError`` to ``MutationResult``
        with ``error_kind="value"``.  The route returns 400 so a client
        does not retry as a different role.
        """
        pipeline_id, worktree = fake_worktree
        _seed_contract(worktree, pipeline_id)

        response = self._mutate(
            client,
            pipeline_id,
            role="implementer",
            body_overrides={
                "field_path": "phases.0.status",
                "new_value": "complete",
            },
        )
        assert response.status_code == 400
        assert "Failed to apply mutation" in json.loads(response.data)["message"]

    def test_invalid_value_returns_400(self, client, fake_worktree):
        """#2495: out-of-domain value (e.g. arbitrary string for a
        ``PipelinePhase`` enum) is a value error, not authorization.

        Reviewer is authorized to mutate ``current_phase``, but pydantic's
        ``validate_assignment=True`` (added for #2465) raises
        ``ValidationError`` for ``"garbage"``.  ``apply_mutation``
        translates that to ``MutationResult`` with ``error_kind="value"``,
        and the route returns 400.
        """
        pipeline_id, worktree = fake_worktree
        _seed_contract(worktree, pipeline_id)

        response = self._mutate(
            client,
            pipeline_id,
            role="reviewer",
            body_overrides={
                "field_path": "current_phase",
                "new_value": "garbage",
            },
        )
        assert response.status_code == 400
        assert "Invalid value for current_phase" in json.loads(response.data)["message"]


class TestValidateMutation:
    def test_allowed(self, client):
        response = client.post(
            "/api/v1/contract-mutations/validate",
            json={"field_path": "phases.0.status", "new_value": "complete"},
            headers={"X-Egg-Role": "implementer"},
        )
        assert response.status_code == 200

    def test_missing_role(self, client):
        response = client.post(
            "/api/v1/contract-mutations/validate",
            json={"field_path": "phases.0.status", "new_value": "complete"},
        )
        assert response.status_code == 403

    def test_missing_field_path(self, client):
        response = client.post(
            "/api/v1/contract-mutations/validate",
            json={"new_value": "complete"},
            headers={"X-Egg-Role": "implementer"},
        )
        assert response.status_code == 400


class TestBranchReadFallback:
    """Covers the #1977 branch-read fallback path for finished pipelines.

    Once a pipeline completes and its shared worktree is pruned, the
    committed contract on the feature branch is the only remaining copy.
    The GET paths fall back to ``git show <branch>:.egg-state/contracts/…``
    via ``contract_store.load_contract_from_branch``.
    """

    PIPELINE_ID = "issue-1977"

    def _fake_pipeline(self, tmp_path: Path, branch: str | None = "egg/issue-1977"):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        store = SimpleNamespace(repo_path=repo_path)
        pipeline = SimpleNamespace(branch=branch)
        return store, pipeline

    def _contract_json(self) -> str:
        contract = Contract(pipeline_id=self.PIPELINE_ID)
        return json.dumps(contract.model_dump(mode="json"), default=str)

    def test_get_falls_back_to_branch_when_worktree_missing(self, client, tmp_path, monkeypatch):
        # Force resolve_pipeline_worktree to miss.
        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", tmp_path / "nowhere")

        store, pipeline = self._fake_pipeline(tmp_path)
        mock_subproc = MagicMock()
        mock_subproc.stdout = self._contract_json()

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("subprocess.run", return_value=mock_subproc) as run_mock,
        ):
            response = client.get(
                f"/api/v1/contracts/{self.PIPELINE_ID}",
                query_string={"pipeline_id": self.PIPELINE_ID},
            )

        assert response.status_code == 200, response.data
        body = json.loads(response.data)
        assert body["success"] is True
        assert body["source"] == "branch"
        assert body["data"]["pipeline_id"] == self.PIPELINE_ID
        # Preferred ref is origin/<branch> — that's what a main-repo
        # checkout sees after the worktree pushed the final commit.
        assert run_mock.call_args_list[0].args[0] == [
            "git",
            "show",
            "origin/egg/issue-1977:.egg-state/contracts/issue-1977.json",
        ]
        assert run_mock.call_args_list[0].kwargs["cwd"] == store.repo_path

    def test_get_prefers_worktree_when_both_are_available(self, client, tmp_path, monkeypatch):
        """When the worktree exists, the branch fallback must not fire."""
        pipeline_id = self.PIPELINE_ID
        worktrees_base = tmp_path / "worktrees"
        worktree = worktrees_base / pipeline_id / "egg"
        worktree.mkdir(parents=True)
        (worktree / ".git").mkdir()
        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", worktrees_base)
        _seed_contract(worktree, pipeline_id)

        with patch("subprocess.run") as run_mock:
            response = client.get(
                f"/api/v1/contracts/{pipeline_id}",
                query_string={"pipeline_id": pipeline_id, "repo": "egg"},
            )

        assert response.status_code == 200
        assert json.loads(response.data)["source"] == "worktree"
        run_mock.assert_not_called()

    def test_get_returns_404_when_worktree_and_branch_both_miss(
        self, client, tmp_path, monkeypatch
    ):
        import subprocess

        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", tmp_path / "nowhere")
        store, pipeline = self._fake_pipeline(tmp_path)

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(128, "git show"),
            ),
        ):
            response = client.get(
                f"/api/v1/contracts/{self.PIPELINE_ID}",
                query_string={"pipeline_id": self.PIPELINE_ID},
            )

        assert response.status_code == 404
        body = json.loads(response.data)
        assert body["success"] is False
        assert "worktree not found" in body["message"].lower()

    def test_get_returns_404_when_pipeline_record_missing(self, client, tmp_path, monkeypatch):
        from state_store import PipelineNotFoundError

        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", tmp_path / "nowhere")

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                side_effect=PipelineNotFoundError("nope"),
            ),
            patch("subprocess.run") as run_mock,
        ):
            response = client.get(
                f"/api/v1/contracts/{self.PIPELINE_ID}",
                query_string={"pipeline_id": self.PIPELINE_ID},
            )

        assert response.status_code == 404
        # We should not shell out to git when the pipeline record is missing.
        run_mock.assert_not_called()

    def test_exists_falls_back_to_branch(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", tmp_path / "nowhere")

        store, pipeline = self._fake_pipeline(tmp_path)
        mock_subproc = MagicMock()
        mock_subproc.stdout = self._contract_json()

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("subprocess.run", return_value=mock_subproc),
        ):
            response = client.get(
                f"/api/v1/contracts/{self.PIPELINE_ID}/exists",
                query_string={"pipeline_id": self.PIPELINE_ID},
            )

        assert response.status_code == 200
        body = json.loads(response.data)
        assert body["data"] == {"exists": True}
        assert body["source"] == "branch"

    def test_get_uses_derived_branch_when_pipeline_branch_is_none(
        self, client, tmp_path, monkeypatch
    ):
        """When pipeline.branch is None, fallback derives egg/<pipeline_id>."""
        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", tmp_path / "nowhere")

        store, pipeline = self._fake_pipeline(tmp_path, branch=None)
        mock_subproc = MagicMock()
        mock_subproc.stdout = self._contract_json()

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch("subprocess.run", return_value=mock_subproc) as run_mock,
        ):
            response = client.get(
                f"/api/v1/contracts/{self.PIPELINE_ID}",
                query_string={"pipeline_id": self.PIPELINE_ID},
            )

        assert response.status_code == 200, response.data
        body = json.loads(response.data)
        assert body["source"] == "branch"
        # With branch=None the code should derive "egg/<pipeline_id>/work"
        # (the /work-suffixed shape from #2399) and try
        # origin/egg/<pipeline_id>/work as the preferred ref.
        assert run_mock.call_args_list[0].args[0] == [
            "git",
            "show",
            f"origin/egg/{self.PIPELINE_ID}/work:.egg-state/contracts/{self.PIPELINE_ID}.json",
        ]
        assert run_mock.call_args_list[0].kwargs["cwd"] == store.repo_path

    def test_exists_returns_404_when_worktree_and_branch_both_miss(
        self, client, tmp_path, monkeypatch
    ):
        import subprocess

        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", tmp_path / "nowhere")
        store, pipeline = self._fake_pipeline(tmp_path)

        with (
            patch(
                "routes.get_state_store_for_pipeline",
                return_value=(store, pipeline),
            ),
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(128, "git show"),
            ),
        ):
            response = client.get(
                f"/api/v1/contracts/{self.PIPELINE_ID}/exists",
                query_string={"pipeline_id": self.PIPELINE_ID},
            )

        assert response.status_code == 404
        body = json.loads(response.data)
        assert body["success"] is False
        assert "worktree not found" in body["message"].lower()

    def test_mutate_still_404_when_worktree_missing(self, client, tmp_path, monkeypatch):
        """Mutations must not use the branch fallback — writes require a live worktree."""
        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", tmp_path / "nowhere")

        response = client.post(
            f"/api/v1/contracts/{self.PIPELINE_ID}/mutate",
            json={
                "pipeline_id": self.PIPELINE_ID,
                "field_path": "phases",
                "new_value": [],
            },
            headers={"X-Egg-Role": "implementer"},
        )
        assert response.status_code == 404


class TestResolvePipelineWorktree:
    def test_returns_worktree_when_present(self, tmp_path, monkeypatch):
        base = tmp_path / "worktrees"
        wt = base / "issue-42" / "egg"
        wt.mkdir(parents=True)
        (wt / ".git").mkdir()

        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", base)

        assert contract_store.resolve_pipeline_worktree("issue-42") == wt

    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", tmp_path / "worktrees")
        assert contract_store.resolve_pipeline_worktree("nope") is None

    def test_empty_id_returns_none(self):
        assert contract_store.resolve_pipeline_worktree("") is None

    @pytest.mark.parametrize(
        "pipeline_id",
        ["../../etc", "../other", "has/slash", "has..", "a b"],
    )
    def test_path_traversal_pipeline_id_rejected(self, pipeline_id):
        assert contract_store.resolve_pipeline_worktree(pipeline_id) is None

    def test_path_traversal_repo_hint_rejected(self, tmp_path, monkeypatch):
        base = tmp_path / "worktrees"
        wt = base / "issue-42" / "egg"
        wt.mkdir(parents=True)
        (wt / ".git").mkdir()
        monkeypatch.setattr(contract_store, "_WORKTREE_BASE_DIR", base)

        assert contract_store.resolve_pipeline_worktree("issue-42", "../../etc") is None
