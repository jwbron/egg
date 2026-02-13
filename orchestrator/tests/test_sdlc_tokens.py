"""
Tests for SDLC token-gated approval endpoints and word list.
"""

import hashlib
import json
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
