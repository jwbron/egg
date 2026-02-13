"""
Tests for SDLC token-gated approval endpoints and word list.
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sdlc_wordlist import WORD_LIST


class TestWordList:
    """Tests for the SDLC word list."""

    def test_minimum_word_count(self):
        """Word list should have at least 200 words for sufficient entropy."""
        assert len(WORD_LIST) >= 200

    def test_no_duplicates(self):
        """Word list should have no duplicates."""
        assert len(WORD_LIST) == len(set(WORD_LIST))

    def test_word_length(self):
        """All words should be 2-7 characters."""
        for word in WORD_LIST:
            assert 2 <= len(word) <= 7, f"Word '{word}' is {len(word)} chars (expected 2-7)"

    def test_all_uppercase(self):
        """All words should be uppercase."""
        for word in WORD_LIST:
            assert word == word.upper(), f"Word '{word}' is not uppercase"

    def test_all_alpha(self):
        """All words should contain only letters."""
        for word in WORD_LIST:
            assert word.isalpha(), f"Word '{word}' contains non-alpha characters"


@pytest.fixture()
def app():
    """Create a minimal Flask app with just the sdlc_tokens blueprint."""
    from routes.sdlc_tokens import _token_store, sdlc_tokens_bp

    test_app = Flask(__name__)
    test_app.register_blueprint(sdlc_tokens_bp)
    _token_store.clear()
    yield test_app
    _token_store.clear()


@pytest.fixture()
def client(app):
    """Create a test client."""
    return app.test_client()


class TestTokenGeneration:
    """Tests for token generation endpoint."""

    def test_generate_returns_two_tokens(self, client):
        """Generate should return refine and plan tokens."""
        resp = client.post(
            "/api/v1/sdlc-tokens/generate",
            json={"pipeline_id": "issue-100"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "refine_token" in data["data"]
        assert "plan_token" in data["data"]

    def test_token_format(self, client):
        """Tokens should be WORD-WORD-WORD format, uppercase."""
        resp = client.post(
            "/api/v1/sdlc-tokens/generate",
            json={"pipeline_id": "issue-101"},
        )
        data = resp.get_json()["data"]
        for key in ("refine_token", "plan_token"):
            token = data[key]
            assert re.match(r"^[A-Z]+-[A-Z]+-[A-Z]+$", token), f"Bad format: {token}"
            parts = token.split("-")
            assert len(parts) == 3
            for part in parts:
                assert part in WORD_LIST

    def test_tokens_are_different(self, client):
        """Refine and plan tokens should be different."""
        resp = client.post(
            "/api/v1/sdlc-tokens/generate",
            json={"pipeline_id": "issue-102"},
        )
        data = resp.get_json()["data"]
        assert data["refine_token"] != data["plan_token"]

    def test_generate_missing_pipeline_id(self, client):
        """Generate without pipeline_id should return 400."""
        resp = client.post(
            "/api/v1/sdlc-tokens/generate",
            json={},
        )
        assert resp.status_code == 400

    def test_generate_duplicate_pipeline(self, client):
        """Generating tokens twice for same pipeline should return 409."""
        client.post(
            "/api/v1/sdlc-tokens/generate",
            json={"pipeline_id": "issue-103"},
        )
        resp = client.post(
            "/api/v1/sdlc-tokens/generate",
            json={"pipeline_id": "issue-103"},
        )
        assert resp.status_code == 409


class TestTokenApproval:
    """Tests for token approval endpoint."""

    @pytest.fixture(autouse=True)
    def setup_tokens(self, client):
        """Generate tokens for testing."""
        resp = client.post(
            "/api/v1/sdlc-tokens/generate",
            json={"pipeline_id": "issue-200"},
        )
        data = resp.get_json()["data"]
        self.refine_token = data["refine_token"]
        self.plan_token = data["plan_token"]

    def test_approve_correct_refine_token(self, client):
        """Approving with correct refine token should return 200."""
        resp = client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-200",
                "phase": "refine",
                "token": self.refine_token,
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["data"]["phase"] == "refine"

    def test_approve_correct_plan_token(self, client):
        """Approving with correct plan token should return 200."""
        resp = client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-200",
                "phase": "plan",
                "token": self.plan_token,
            },
        )
        assert resp.status_code == 200

    def test_approve_wrong_token(self, client):
        """Approving with wrong token should return 403."""
        resp = client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-200",
                "phase": "refine",
                "token": "WRONG-BAD-TOKEN",
            },
        )
        assert resp.status_code == 403

    def test_approve_used_token(self, client):
        """Using same token twice should return 409."""
        client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-200",
                "phase": "refine",
                "token": self.refine_token,
            },
        )
        resp = client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-200",
                "phase": "refine",
                "token": self.refine_token,
            },
        )
        assert resp.status_code == 409

    def test_approve_no_tokens_for_pipeline(self, client):
        """Approving for unknown pipeline should return 404."""
        resp = client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-999",
                "phase": "refine",
                "token": "SOME-TOKEN-HERE",
            },
        )
        assert resp.status_code == 404

    def test_approve_invalid_phase(self, client):
        """Approving with invalid phase should return 400."""
        resp = client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-200",
                "phase": "implement",
                "token": self.refine_token,
            },
        )
        assert resp.status_code == 400

    def test_approve_missing_fields(self, client):
        """Approving with missing fields should return 400."""
        for missing in ("pipeline_id", "phase", "token"):
            payload = {
                "pipeline_id": "issue-200",
                "phase": "refine",
                "token": self.refine_token,
            }
            del payload[missing]
            resp = client.post(
                "/api/v1/sdlc-tokens/approve",
                json=payload,
            )
            assert resp.status_code == 400, f"Expected 400 when missing {missing}"

    def test_approve_case_insensitive(self, client):
        """Token validation should be case-insensitive."""
        resp = client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-200",
                "phase": "refine",
                "token": self.refine_token.lower(),
            },
        )
        assert resp.status_code == 200

    def test_approve_cross_phase_token_rejected(self, client):
        """Using refine token for plan phase should fail."""
        resp = client.post(
            "/api/v1/sdlc-tokens/approve",
            json={
                "pipeline_id": "issue-200",
                "phase": "plan",
                "token": self.refine_token,
            },
        )
        assert resp.status_code == 403


class TestHasTokensForPipeline:
    """Tests for has_tokens_for_pipeline helper."""

    def test_no_tokens(self, app):
        """Should return False when no tokens exist."""
        from routes.sdlc_tokens import has_tokens_for_pipeline

        assert has_tokens_for_pipeline("issue-300") is False

    def test_with_tokens(self, client):
        """Should return True after tokens are generated."""
        from routes.sdlc_tokens import has_tokens_for_pipeline

        client.post(
            "/api/v1/sdlc-tokens/generate",
            json={"pipeline_id": "issue-300"},
        )
        assert has_tokens_for_pipeline("issue-300") is True


class TestDecisionGating:
    """Tests for resolve endpoint gating on token-gated pipelines."""

    def test_pipeline_model_has_sdlc_token_gated_field(self):
        """Pipeline model should have sdlc_token_gated field defaulting to False."""
        from models import Pipeline

        pipeline = Pipeline(id="issue-400", issue_number=400, repo="owner/repo", branch="egg/test")
        assert pipeline.sdlc_token_gated is False

    def test_pipeline_model_accepts_sdlc_token_gated_true(self):
        """Pipeline model should accept sdlc_token_gated=True."""
        from models import Pipeline

        pipeline = Pipeline(
            id="issue-401",
            issue_number=401,
            repo="owner/repo",
            branch="egg/test",
            sdlc_token_gated=True,
        )
        assert pipeline.sdlc_token_gated is True

    def test_pipeline_serialization_includes_sdlc_token_gated(self):
        """Pipeline serialization should include sdlc_token_gated field."""
        from models import Pipeline

        pipeline = Pipeline(
            id="issue-402",
            issue_number=402,
            repo="owner/repo",
            branch="egg/test",
            sdlc_token_gated=True,
        )
        data = pipeline.model_dump()
        assert "sdlc_token_gated" in data
        assert data["sdlc_token_gated"] is True


class TestDecisionGateFailClosed:
    """Tests for the fail-closed behavior of the token gate in resolve_decision."""

    def test_token_gated_pipeline_blocks_direct_resolution(self):
        """resolve_decision should return 403 for token-gated pipelines."""
        from routes.decisions import decisions_bp

        test_app = Flask(__name__)
        test_app.register_blueprint(decisions_bp)

        mock_pipeline = MagicMock()
        mock_pipeline.sdlc_token_gated = True

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = mock_pipeline

        with test_app.test_client() as c:
            with patch("routes.decisions.get_state_store", return_value=mock_store), \
                 patch("routes.decisions.get_repo_path", return_value="/tmp/test"):
                resp = c.post(
                    "/api/v1/pipelines/issue-500/decisions/decision-1/resolve",
                    json={"resolution": "approve"},
                )
                assert resp.status_code == 403
                assert "token-gated" in resp.get_json()["message"]

    def test_state_store_exception_returns_503(self):
        """resolve_decision should return 503 (fail closed) on state store errors."""
        from routes.decisions import decisions_bp

        test_app = Flask(__name__)
        test_app.register_blueprint(decisions_bp)

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = RuntimeError("connection lost")

        with test_app.test_client() as c:
            with patch("routes.decisions.get_state_store", return_value=mock_store), \
                 patch("routes.decisions.get_repo_path", return_value="/tmp/test"):
                resp = c.post(
                    "/api/v1/pipelines/issue-500/decisions/decision-1/resolve",
                    json={"resolution": "approve"},
                )
                assert resp.status_code == 503
                assert "Unable to verify" in resp.get_json()["message"]

    def test_pipeline_not_found_allows_resolution(self):
        """resolve_decision should allow resolution when pipeline doesn't exist."""
        from routes.decisions import decisions_bp
        from state_store import PipelineNotFoundError

        test_app = Flask(__name__)
        test_app.register_blueprint(decisions_bp)

        mock_store = MagicMock()
        mock_store.load_pipeline.side_effect = PipelineNotFoundError("not found")

        mock_decision = MagicMock()
        mock_decision.id = "decision-1"
        mock_decision.question = "test?"
        mock_decision.status.value = "resolved"
        mock_decision.resolution = "approve"
        mock_decision.resolved_at = None

        mock_queue = MagicMock()
        mock_queue.resolve_decision.return_value = mock_decision

        with test_app.test_client() as c:
            with patch("routes.decisions.get_state_store", return_value=mock_store), \
                 patch("routes.decisions.get_repo_path", return_value="/tmp/test"), \
                 patch("routes.decisions.get_decision_queue", return_value=mock_queue):
                resp = c.post(
                    "/api/v1/pipelines/issue-500/decisions/decision-1/resolve",
                    json={"resolution": "approve"},
                )
                assert resp.status_code == 200


class TestPhaseTransitionDecisionMatching:
    """Tests for _is_phase_transition_decision matching logic."""

    def test_matches_exact_phase_transition_question(self):
        """Should match the standard phase transition question format."""
        from routes.sdlc_tokens import _is_phase_transition_decision

        decision = MagicMock()
        decision.question = "The refine phase has completed. Please review the analysis and approve to continue."
        assert _is_phase_transition_decision(decision, "refine") is True

    def test_does_not_match_different_phase(self):
        """Should not match when checking for a different phase."""
        from routes.sdlc_tokens import _is_phase_transition_decision

        decision = MagicMock()
        decision.question = "The refine phase has completed. Please review the analysis and approve to continue."
        assert _is_phase_transition_decision(decision, "plan") is False

    def test_does_not_match_unrelated_question_containing_phase_name(self):
        """Should not match questions that happen to contain the phase name."""
        from routes.sdlc_tokens import _is_phase_transition_decision

        decision = MagicMock()
        decision.question = "What's the test plan?"
        assert _is_phase_transition_decision(decision, "plan") is False

    def test_does_not_match_ambiguous_question(self):
        """Should not match 'Should we refine the plan?' for either phase."""
        from routes.sdlc_tokens import _is_phase_transition_decision

        decision = MagicMock()
        decision.question = "Should we refine the plan?"
        assert _is_phase_transition_decision(decision, "refine") is False
        assert _is_phase_transition_decision(decision, "plan") is False

    def test_matches_plan_phase_transition(self):
        """Should match the plan phase transition question."""
        from routes.sdlc_tokens import _is_phase_transition_decision

        decision = MagicMock()
        decision.question = "The plan phase has completed. Please review the plan and approve to continue."
        assert _is_phase_transition_decision(decision, "plan") is True


class TestResolvePhaseDecisions:
    """Tests for _resolve_phase_decisions auto-resolution."""

    def test_resolves_matching_decision(self):
        """Should resolve a decision that matches the phase transition pattern."""
        from routes.sdlc_tokens import _resolve_phase_decisions

        mock_decision = MagicMock()
        mock_decision.id = "decision-1"
        mock_decision.question = "The refine phase has completed. Please review the analysis and approve to continue."

        mock_queue = MagicMock()
        mock_queue.get_pending_decisions.return_value = [mock_decision]

        with patch("routes.get_repo_path", return_value="/tmp/test"), \
             patch("decision_queue.get_decision_queue", return_value=mock_queue):
            _resolve_phase_decisions("issue-600", "refine")

        mock_queue.resolve_decision.assert_called_once_with(
            "decision-1", "Approved via SDLC token (refine)"
        )

    def test_does_not_resolve_unrelated_decision(self):
        """Should not resolve decisions that don't match the phase pattern."""
        from routes.sdlc_tokens import _resolve_phase_decisions

        mock_decision = MagicMock()
        mock_decision.id = "decision-1"
        mock_decision.question = "What's the test plan for this feature?"

        mock_queue = MagicMock()
        mock_queue.get_pending_decisions.return_value = [mock_decision]

        with patch("routes.get_repo_path", return_value="/tmp/test"), \
             patch("decision_queue.get_decision_queue", return_value=mock_queue):
            _resolve_phase_decisions("issue-600", "plan")

        mock_queue.resolve_decision.assert_not_called()

    def test_handles_exception_gracefully(self):
        """Should not raise when decision resolution fails."""
        from routes.sdlc_tokens import _resolve_phase_decisions

        with patch("routes.get_repo_path", side_effect=RuntimeError("no repo")):
            # Should not raise
            _resolve_phase_decisions("issue-600", "refine")


class TestResetEndpoint:
    """Tests for the /reset endpoint."""

    def test_reset_clears_in_memory_tokens(self, client):
        """Reset should remove tokens from the in-memory store."""
        from routes.sdlc_tokens import has_tokens_for_pipeline

        client.post(
            "/api/v1/sdlc-tokens/generate",
            json={"pipeline_id": "issue-700"},
        )
        assert has_tokens_for_pipeline("issue-700") is True

        resp = client.post(
            "/api/v1/sdlc-tokens/reset",
            json={"pipeline_id": "issue-700"},
        )
        assert resp.status_code == 200
        assert has_tokens_for_pipeline("issue-700") is False

    def test_reset_missing_pipeline_id(self, client):
        """Reset without pipeline_id should return 400."""
        resp = client.post(
            "/api/v1/sdlc-tokens/reset",
            json={},
        )
        assert resp.status_code == 400

    def test_reset_nonexistent_pipeline(self, client):
        """Reset for unknown pipeline should succeed (idempotent)."""
        resp = client.post(
            "/api/v1/sdlc-tokens/reset",
            json={"pipeline_id": "issue-999"},
        )
        assert resp.status_code == 200
