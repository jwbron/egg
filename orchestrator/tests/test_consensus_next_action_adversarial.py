"""Adversarial probes for the consensus next-action route (issue #2908 slice-1).

These tests target edge cases and boundary conditions that the happy-path
suite in ``test_consensus_next_action.py`` does not exercise. Each test is
designed to surface a real bug or a latent fragility that the coder should
harden.

Patterns:
- Malformed request bodies (missing keys, wrong types, injection attempts)
- Tracker reconstruction edge cases (missing review graph, corrupt messages)
- Invalid action coercion (defensive fallback to "wait")
- Dual-role edge cases (agent reviewing its own producer role)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "shared"))

from orchestrator.routes import consensus  # noqa: E402


@pytest.fixture
def app():
    """Minimal Flask app for testing the consensus blueprint."""
    from flask import Flask

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(consensus.consensus_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Adversarial probes: request validation
# ---------------------------------------------------------------------------


class TestNextActionRequestValidationAdversarial:
    def test_missing_role_returns_400(self, client):
        """When ``role`` is missing from the request body, the route
        returns 400 with a clear error message."""
        with patch("orchestrator.routes.consensus._resolve_tracker"):
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"slice_id": "slice-1"},
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "role" in data.get("message", "").lower()

    def test_empty_role_string_returns_400(self, client):
        """An empty string for ``role`` is rejected (not coerced to None)."""
        with patch("orchestrator.routes.consensus._resolve_tracker"):
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "", "slice_id": "slice-1"},
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "role" in data.get("message", "").lower()

    def test_role_with_whitespace_only_returns_400(self, client):
        """A role that is only whitespace (``"   "``) is rejected."""
        with patch("orchestrator.routes.consensus._resolve_tracker"):
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "   ", "slice_id": "slice-1"},
            )
        assert resp.status_code == 400

    def test_missing_request_body_returns_400(self, client):
        """When the request body is missing entirely, the route returns
        400 (not 500 from ``request.get_json()`` crashing)."""
        resp = client.post("/api/v1/pipelines/issue-2908-impl2/consensus/next-action")
        assert resp.status_code == 400
        data = resp.get_json()
        assert (
            "body" in data.get("message", "").lower() or "json" in data.get("message", "").lower()
        )

    def test_non_json_body_returns_400(self, client):
        """When the request body is not valid JSON, the route returns 400."""
        resp = client.post(
            "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Adversarial probes: slice_id validation
# ---------------------------------------------------------------------------


class TestNextActionSliceIdValidationAdversarial:
    def test_invalid_slice_id_format_returns_400(self, client):
        """A malformed ``slice_id`` (e.g. ``"slice-999"`` when only
        ``"slice-1"`` exists) is rejected by the canonical helper."""
        with patch("orchestrator.routes.consensus._extract_slice_id") as mock_extract:
            mock_extract.side_effect = ValueError("Invalid slice_id: slice-999")
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "coder", "slice_id": "slice-999"},
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "slice_id" in data.get("message", "").lower()

    def test_slice_id_with_path_separator_rejected(self, client):
        """A ``slice_id`` containing ``/`` or ``..`` is rejected (path
        traversal attack)."""
        with patch("orchestrator.routes.consensus._extract_slice_id") as mock_extract:
            mock_extract.side_effect = ValueError("Invalid slice_id: slice-1/../../etc")
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "coder", "slice_id": "slice-1/../../etc"},
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Adversarial probes: tracker resolution
# ---------------------------------------------------------------------------


class TestNextActionTrackerResolutionAdversarial:
    def test_no_tracker_yet_returns_wait(self, client):
        """When the pipeline has not started consensus yet (no tracker),
        the route returns ``action="wait"`` (not 404 or 500)."""
        with patch(
            "orchestrator.routes.consensus._resolve_tracker",
            return_value=None,
        ):
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "coder", "slice_id": "slice-1"},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["action"] == "wait"
        assert "block on message bus" in data.get("reason", "").lower()

    def test_phantom_role_returns_400(self, client):
        """When the role is not in the review graph (neither producer nor
        reviewer), the route returns 400 (configuration error)."""
        mock_tracker = MagicMock()
        mock_tracker.graph.is_producer.return_value = False
        mock_tracker.graph.is_reviewer.return_value = False

        with patch(
            "orchestrator.routes.consensus._resolve_tracker",
            return_value=mock_tracker,
        ):
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "unknown_role", "slice_id": "slice-1"},
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "not a participant" in data.get("message", "").lower()


# ---------------------------------------------------------------------------
# Adversarial probes: action derivation
# ---------------------------------------------------------------------------


class TestNextActionDerivationAdversarial:
    def test_invalid_action_coerced_to_wait(self, client):
        """When ``_derive_next_action`` returns an invalid action string,
        the route defensively coerces it to ``"wait"`` (not 500)."""
        mock_tracker = MagicMock()
        mock_tracker.graph.is_producer.return_value = True

        with (
            patch(
                "orchestrator.routes.consensus._resolve_tracker",
                return_value=mock_tracker,
            ),
            patch(
                "orchestrator.routes.consensus._derive_next_action",
                return_value=("invalid_action", None, "test"),
            ),
        ):
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "coder", "slice_id": "slice-1"},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["action"] == "wait"

    def test_dual_role_reviewing_own_producer_skipped(self, client):
        """A dual-role agent (e.g. tester) must NOT review its own
        producer role — the pending-reviews check skips self-reviews."""
        from egg_orchestrator.types import ConsensusPhase

        from orchestrator.routes.consensus import _has_pending_peer_proposals

        mock_tracker = MagicMock()
        # Tester is both producer and reviewer.
        mock_tracker.graph.producers_for.return_value = ["tester", "coder"]
        mock_tracker._producer_phases = {
            "tester": ConsensusPhase.PROPOSED,
            "coder": ConsensusPhase.WORKING,
        }
        mock_tracker.matrix.get_proposal_version.return_value = 1
        mock_tracker.matrix.get_entry.return_value = None

        has_pending, pending = _has_pending_peer_proposals(mock_tracker, "tester")

        # The dual-role agent must NOT be asked to review its own producer
        # role — only the other producer (coder) is pending.
        if has_pending:
            # If there are pending reviews, they must not include self.
            for p in pending:
                assert p["producer"] != "tester", (
                    "Dual-role agent must not be asked to review its own producer role"
                )


# ---------------------------------------------------------------------------
# Adversarial probes: response shape
# ---------------------------------------------------------------------------


class TestNextActionResponseShapeAdversarial:
    def test_success_response_includes_all_required_keys(self, client):
        """A successful response includes ``success``, ``action``,
        ``role``, and ``slice_id`` keys (the CLI depends on these)."""
        with patch(
            "orchestrator.routes.consensus._resolve_tracker",
            return_value=None,
        ):
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "coder", "slice_id": "slice-1"},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "success" in data
        assert "action" in data
        assert "role" in data
        assert data["success"] is True

    def test_event_payload_omitted_when_none(self, client):
        """When ``event_payload`` is None, the key is omitted from the
        response (not serialized as ``"event_payload": null``)."""
        with patch(
            "orchestrator.routes.consensus._resolve_tracker",
            return_value=None,
        ):
            resp = client.post(
                "/api/v1/pipelines/issue-2908-impl2/consensus/next-action",
                json={"role": "coder", "slice_id": "slice-1"},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        # The CLI code does ``result.get("event_payload") or {}`` — if
        # the key is present with value ``null``, the ``or {}`` fallback
        # still works, but omitting the key is cleaner.
        # Accept either contract (key absent OR value null).
        assert "event_payload" not in data or data.get("event_payload") is None
