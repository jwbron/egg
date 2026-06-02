"""Tests for orchestrator/routes/commit_authorship.py (issue #1882).

Covers TASK-5-2 acceptance:

- 401 without the inter-pod shared secret
- 400 on malformed input
- idempotent registration (200)
- 409 on first-wins collision
- 200 happy-path round trip through /register and /lookup
- 500 surface when the store is down (without leaking internals)

The routes are gated by ``require_lifecycle_secret``.  The
conftest auto-injects the Bearer token on every FlaskClient.open call,
so tests get the happy path for free; auth-failure tests pass
``_lifecycle_auth=False``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


_VALID_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_THIRD_SHA = "c" * 40


@pytest.fixture
def store(tmp_path: Path):
    """An isolated filesystem-backed authorship store for each test."""
    from commit_authorship_store import (  # type: ignore[import-not-found]
        CommitAuthorshipStore,
        reset_singleton,
    )

    reset_singleton()
    return CommitAuthorshipStore(worktree_dir=tmp_path / "wt")


@pytest.fixture
def app(store, monkeypatch):
    """Flask app with the commit_authorship blueprint and the store injected."""
    # Point the route module's get_store() at our isolated store.
    import routes.commit_authorship as route_mod  # type: ignore[import-not-found]
    from flask import Flask

    monkeypatch.setattr(route_mod, "get_store", lambda: store)

    app = Flask(__name__)
    app.register_blueprint(route_mod.commit_authorship_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_register_without_secret_returns_401(self, client):
        """No Authorization header -> 401."""
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder"}),
            content_type="application/json",
            _lifecycle_auth=False,
        )
        assert response.status_code == 401
        body = response.get_json()
        assert body["success"] is False

    def test_register_with_wrong_secret_returns_401(self, client):
        """Wrong Bearer token -> 401."""
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder"}),
            content_type="application/json",
            headers={"Authorization": "Bearer wrong-secret"},
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_lookup_without_secret_returns_401(self, client):
        """Lookup is also gated by the shared secret."""
        response = client.post(
            "/api/v1/commit-authorship/lookup",
            data=json.dumps({"shas": [_VALID_SHA]}),
            content_type="application/json",
            _lifecycle_auth=False,
        )
        assert response.status_code == 401

    def test_register_bulk_without_secret_returns_401(self, client):
        """register-bulk is also gated."""
        response = client.post(
            "/api/v1/commit-authorship/register-bulk",
            data=json.dumps({"items": []}),
            content_type="application/json",
            _lifecycle_auth=False,
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# /register — happy path and malformed input
# ---------------------------------------------------------------------------


class TestRegisterEndpoint:
    def test_register_happy_path_returns_200(self, client, store):
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder", "pipeline_id": "issue-1882"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["sha"] == _VALID_SHA
        assert body["role"] == "coder"
        assert body["inserted"] is True
        assert store.lookup(_VALID_SHA) == "coder"

    def test_register_is_idempotent(self, client):
        """Second identical request returns 200 with ``inserted=False``."""
        for _ in range(2):
            response = client.post(
                "/api/v1/commit-authorship/register",
                data=json.dumps({"sha": _VALID_SHA, "role": "coder", "pipeline_id": "issue-1882"}),
                content_type="application/json",
            )
            assert response.status_code == 200
        assert response.get_json()["inserted"] is False

    def test_register_collision_returns_409(self, client):
        """Different role for same SHA returns 409 with collision details."""
        client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder", "pipeline_id": "issue-1882"}),
            content_type="application/json",
        )
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "tester", "pipeline_id": "issue-1882"}),
            content_type="application/json",
        )
        assert response.status_code == 409
        body = response.get_json()
        assert body["success"] is False
        assert body["sha"] == _VALID_SHA
        assert body["existing_role"] == "coder"
        assert body["attempted_role"] == "tester"

    def test_register_missing_body_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register",
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_register_non_json_body_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register",
            data="not json",
        )
        assert response.status_code == 400

    def test_register_missing_sha_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"role": "coder"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_register_missing_role_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_register_non_string_sha_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": 12345, "role": "coder"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_register_non_string_pipeline_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder", "pipeline_id": 1882}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_register_invalid_sha_format_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": "not-a-sha", "role": "coder"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_register_store_error_returns_500(self, client, monkeypatch):
        """A bare Exception from the store surfaces as 500 (not a leak of internals)."""
        import routes.commit_authorship as route_mod  # type: ignore[import-not-found]

        class _BoomStore:
            def register(self, *_a, **_kw):
                raise RuntimeError("simulated backing-store failure")

        monkeypatch.setattr(route_mod, "get_store", lambda: _BoomStore())
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder", "pipeline_id": "issue-1882"}),
            content_type="application/json",
        )
        assert response.status_code == 500
        body = response.get_json()
        # Generic message — no internal details.
        assert "simulated backing-store failure" not in body["message"]

    def test_register_publishes_container_activity(self, client, monkeypatch):
        """A successful registration publishes a CONTAINER_ACTIVITY event so
        HealthMonitor can suppress heartbeat/progress alerts against an
        agent that is demonstrably alive (#2190)."""
        # Reroute get_event_bus inside the route module's helper. The route
        # imports get_event_bus lazily inside _publish_container_activity,
        # so patching the events module is sufficient.
        import events as events_mod  # type: ignore[import-not-found]
        from events import EventBus, EventType  # type: ignore[import-not-found]

        bus = EventBus(async_delivery=False)
        monkeypatch.setattr(events_mod, "get_event_bus", lambda: bus)

        captured: list = []
        bus.subscribe(EventType.CONTAINER_ACTIVITY, lambda e: captured.append(e))

        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder", "pipeline_id": "issue-1882"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert len(captured) == 1
        evt = captured[0]
        assert evt.pipeline_id == "issue-1882"
        assert evt.data["agent_role"] == "coder"
        assert evt.data["kind"] == "git_commit"

    def test_register_without_pipeline_id_does_not_publish(self, client, monkeypatch):
        """Orphan registrations (no pipeline_id) do not publish CONTAINER_ACTIVITY —
        the event has no pipeline scope to attach to."""
        import events as events_mod  # type: ignore[import-not-found]
        from events import EventBus, EventType  # type: ignore[import-not-found]

        bus = EventBus(async_delivery=False)
        monkeypatch.setattr(events_mod, "get_event_bus", lambda: bus)

        captured: list = []
        bus.subscribe(EventType.CONTAINER_ACTIVITY, lambda e: captured.append(e))

        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert captured == []


# ---------------------------------------------------------------------------
# /lookup — batch attribution
# ---------------------------------------------------------------------------


class TestLookupEndpoint:
    def test_lookup_returns_attribution(self, client, store):
        """Returns a {sha: role} map with None for unknown SHAs."""
        store.register(_VALID_SHA, "coder", "issue-1882")
        store.register(_OTHER_SHA, "tester", "issue-1882")
        response = client.post(
            "/api/v1/commit-authorship/lookup",
            data=json.dumps({"shas": [_VALID_SHA, _OTHER_SHA, _THIRD_SHA]}),
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["attribution"][_VALID_SHA] == "coder"
        assert body["attribution"][_OTHER_SHA] == "tester"
        assert body["attribution"][_THIRD_SHA] is None

    def test_lookup_empty_batch(self, client):
        response = client.post(
            "/api/v1/commit-authorship/lookup",
            data=json.dumps({"shas": []}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.get_json()["attribution"] == {}

    def test_lookup_missing_shas_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/lookup",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_lookup_wrong_shas_type_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/lookup",
            data=json.dumps({"shas": "abc"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_lookup_batch_exceeds_cap_returns_400(self, client):
        """> 500 SHAs rejected to avoid memory blow-up."""
        shas = ["a" * 40] * 501
        response = client.post(
            "/api/v1/commit-authorship/lookup",
            data=json.dumps({"shas": shas}),
            content_type="application/json",
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# /register-bulk
# ---------------------------------------------------------------------------


class TestRegisterBulkEndpoint:
    def test_bulk_register_success_and_collision(self, client, store):
        """Individual collisions in a batch don't abort the whole request."""
        # Pre-seed a binding so the second item collides.
        store.register(_OTHER_SHA, "coder", "issue-1882")
        response = client.post(
            "/api/v1/commit-authorship/register-bulk",
            data=json.dumps(
                {
                    "items": [
                        {"sha": _VALID_SHA, "role": "coder", "pipeline_id": "issue-1882"},
                        {"sha": _OTHER_SHA, "role": "tester", "pipeline_id": "issue-1882"},
                        {"sha": _THIRD_SHA, "role": "coder", "pipeline_id": "issue-1882"},
                    ]
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        results = body["results"]
        assert len(results) == 3
        assert results[0]["success"] is True
        assert results[0]["sha"] == _VALID_SHA
        assert results[1]["success"] is False
        assert results[1]["status"] == 409
        assert results[2]["success"] is True

    def test_bulk_register_items_exceed_cap(self, client):
        items = [{"sha": _VALID_SHA, "role": "coder"}] * 101
        response = client.post(
            "/api/v1/commit-authorship/register-bulk",
            data=json.dumps({"items": items}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_bulk_register_missing_items_returns_400(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register-bulk",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_bulk_register_non_dict_item_yields_per_item_error(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register-bulk",
            data=json.dumps({"items": ["not a dict"]}),
            content_type="application/json",
        )
        assert response.status_code == 200
        results = response.get_json()["results"]
        assert len(results) == 1
        assert results[0]["success"] is False

    def test_bulk_register_empty_list_succeeds(self, client):
        response = client.post(
            "/api/v1/commit-authorship/register-bulk",
            data=json.dumps({"items": []}),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.get_json()["results"] == []


# ---------------------------------------------------------------------------
# Error-surface parity — "store unavailable"
# ---------------------------------------------------------------------------


class TestStoreUnavailable:
    def _install_broken_store(self, monkeypatch):
        import routes.commit_authorship as route_mod  # type: ignore[import-not-found]

        def _broken_get_store():
            raise RuntimeError("state store unreachable")

        monkeypatch.setattr(route_mod, "get_store", _broken_get_store)

    def test_register_returns_500_when_store_unavailable(self, client, monkeypatch):
        self._install_broken_store(monkeypatch)
        response = client.post(
            "/api/v1/commit-authorship/register",
            data=json.dumps({"sha": _VALID_SHA, "role": "coder"}),
            content_type="application/json",
        )
        assert response.status_code == 500
        assert "unavailable" in response.get_json()["message"].lower()

    def test_lookup_returns_500_when_store_unavailable(self, client, monkeypatch):
        self._install_broken_store(monkeypatch)
        response = client.post(
            "/api/v1/commit-authorship/lookup",
            data=json.dumps({"shas": [_VALID_SHA]}),
            content_type="application/json",
        )
        assert response.status_code == 500

    def test_bulk_register_returns_500_when_store_unavailable(self, client, monkeypatch):
        self._install_broken_store(monkeypatch)
        response = client.post(
            "/api/v1/commit-authorship/register-bulk",
            data=json.dumps({"items": [{"sha": _VALID_SHA, "role": "coder"}]}),
            content_type="application/json",
        )
        assert response.status_code == 500
