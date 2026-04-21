"""Tests for orchestrator contract endpoints (#1781).

These exercise the single source of truth for contract state:
the orchestrator's ``/api/v1/contracts/…`` endpoints backed by the
shared pipeline worktree.  Gateway interactions are tested
separately in ``gateway/tests/test_contract_api.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import contract_store
import pytest
from api import app
from egg_contracts import create_contract


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
        assert response.status_code in (403, 400)


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
