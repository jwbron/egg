"""Tests for ``_writeback_pr_link_to_jira_child`` (#1557 TASK-1-15).

The implement-phase PR-phase finalizer post-pads a comment back on the
child Jira ticket so the operator can navigate epic → child → PR within
Jira without leaving the tool. The helper must:

- Only fire when ``pipeline.jira_parent_epic_key`` is set.
- Walk recent comments and idempotently short-circuit when the PR URL
  is already present.
- Always pass ``use_launcher_auth=True`` on the gateway request (the
  orchestrator process has no session token; the launcher secret is
  the only auth it has against the gateway — reviewer_code v3 NACK #4).
- Swallow gateway errors so a Jira outage cannot block PR-phase
  completion.

These tests mock the gateway round-trip and assert on the recorded
call args rather than firing any real HTTP.
"""

from unittest.mock import MagicMock, patch

import pytest
from routes.pipelines import _writeback_pr_link_to_jira_child

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    *,
    jira_parent_epic_key: str | None = "ENG-1234",
    jira_ticket: str | None = "ENG-5678",
) -> MagicMock:
    """Build a duck-typed pipeline stand-in.

    The writeback helper only reads two attributes off the pipeline
    (``jira_parent_epic_key`` and ``jira_ticket``) — using a MagicMock
    sidesteps having to instantiate the full Pipeline model and lets us
    poke ``None`` into either field for the adversarial cases.
    """
    pipeline = MagicMock()
    pipeline.jira_parent_epic_key = jira_parent_epic_key
    pipeline.jira_ticket = jira_ticket
    return pipeline


def _comment_response(comments: list[dict]) -> dict:
    """Shape the gateway response the helper expects."""
    return {"data": {"comments": comments}}


# ---------------------------------------------------------------------------
# Trigger condition: only when ``jira_parent_epic_key`` is set.
# ---------------------------------------------------------------------------


class TestWritebackTriggerCondition:
    """The helper is a no-op for non-epic-keyed pipelines."""

    def test_noop_when_jira_parent_epic_key_is_none(self):
        """Missing ``jira_parent_epic_key`` short-circuits before any gateway call."""
        pipeline = _make_pipeline(jira_parent_epic_key=None)

        with patch("gateway_client.GatewayClient") as MockGateway:
            _writeback_pr_link_to_jira_child(pipeline, "https://github.com/owner/repo/pull/99")
            MockGateway.assert_not_called()

    def test_noop_when_jira_parent_epic_key_is_empty_string(self):
        """Empty-string parent epic key is treated as absent (falsy)."""
        pipeline = _make_pipeline(jira_parent_epic_key="")

        with patch("gateway_client.GatewayClient") as MockGateway:
            _writeback_pr_link_to_jira_child(pipeline, "https://github.com/owner/repo/pull/99")
            MockGateway.assert_not_called()

    def test_noop_when_jira_ticket_is_none(self):
        """Even if the epic key is set, no child ticket = no comment to post."""
        pipeline = _make_pipeline(jira_ticket=None)

        with patch("gateway_client.GatewayClient") as MockGateway:
            _writeback_pr_link_to_jira_child(pipeline, "https://github.com/owner/repo/pull/99")
            MockGateway.assert_not_called()

    def test_noop_when_pr_url_is_empty(self):
        """Empty PR URL — nothing to write back."""
        pipeline = _make_pipeline()

        with patch("gateway_client.GatewayClient") as MockGateway:
            _writeback_pr_link_to_jira_child(pipeline, "")
            MockGateway.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path + launcher-auth assertion.
# ---------------------------------------------------------------------------


class TestWritebackHappyPath:
    """When epic-keyed and the comment isn't a duplicate, post the comment."""

    def test_posts_comment_on_child_ticket(self):
        """The helper calls the gateway's comment-add endpoint with the child ticket."""
        pipeline = _make_pipeline()

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            # 1st call: idempotency comments fetch → no prior comments.
            # 2nd call: comment/add → succeeds.
            gateway._make_request.side_effect = [
                _comment_response([]),
                {"ok": True},
            ]

            _writeback_pr_link_to_jira_child(pipeline, "https://github.com/owner/repo/pull/99")

        assert gateway._make_request.call_count == 2
        # 2nd call is the comment add. Confirm endpoint + child key + body
        # mention the PR URL.
        post_call = gateway._make_request.call_args_list[1]
        assert post_call.args[0] == "/api/v1/jira/ticket/comment/add"
        assert post_call.kwargs["method"] == "POST"
        assert post_call.kwargs["data"]["ticket"] == "ENG-5678"
        assert "https://github.com/owner/repo/pull/99" in post_call.kwargs["data"]["body"]

    def test_uses_launcher_auth_on_every_gateway_call(self):
        """``use_launcher_auth=True`` is required on every Jira call (#1557 R7).

        Without it the gateway returns 401 because the orchestrator
        process has no session token.
        """
        pipeline = _make_pipeline()

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            gateway._make_request.side_effect = [
                _comment_response([]),
                {"ok": True},
            ]

            _writeback_pr_link_to_jira_child(pipeline, "https://github.com/owner/repo/pull/99")

        # Every recorded call must carry use_launcher_auth=True.
        for call in gateway._make_request.call_args_list:
            assert call.kwargs.get("use_launcher_auth") is True, (
                f"call {call} did not pass use_launcher_auth=True"
            )

    def test_comment_body_references_parent_epic(self):
        """The auto-link comment also names the parent epic for context."""
        pipeline = _make_pipeline(jira_parent_epic_key="ENG-9999")

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            gateway._make_request.side_effect = [
                _comment_response([]),
                {"ok": True},
            ]

            _writeback_pr_link_to_jira_child(pipeline, "https://github.com/owner/repo/pull/42")

        body = gateway._make_request.call_args_list[1].kwargs["data"]["body"]
        assert "ENG-9999" in body


# ---------------------------------------------------------------------------
# Idempotency: don't duplicate the comment on re-runs.
# ---------------------------------------------------------------------------


class TestWritebackIdempotency:
    """Re-running the writeback must NOT duplicate the comment."""

    def test_skips_when_pr_url_already_in_recent_comment_text(self):
        """A prior plain-text comment containing the PR URL short-circuits."""
        pipeline = _make_pipeline()
        pr_url = "https://github.com/owner/repo/pull/99"

        existing = [
            {"id": "1", "body": "Some unrelated note"},
            {"id": "2", "body": f"Auto-link from egg SDLC pipeline: {pr_url}"},
        ]

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            gateway._make_request.side_effect = [_comment_response(existing)]

            _writeback_pr_link_to_jira_child(pipeline, pr_url)

        # Exactly one call — the comments fetch. No comment/add.
        assert gateway._make_request.call_count == 1
        only_call = gateway._make_request.call_args_list[0]
        assert only_call.args[0] == "/api/v1/jira/ticket/comments"

    def test_skips_when_pr_url_already_in_adf_comment_body(self):
        """An ADF (dict) body containing the PR URL also short-circuits.

        Atlassian Document Format bodies are nested dicts; the helper
        serialises them with json.dumps and substring-matches on the
        PR URL.
        """
        pipeline = _make_pipeline()
        pr_url = "https://github.com/owner/repo/pull/99"

        adf_body = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": f"See PR {pr_url}"}],
                }
            ],
        }
        existing = [{"id": "5", "body": adf_body}]

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            gateway._make_request.side_effect = [_comment_response(existing)]

            _writeback_pr_link_to_jira_child(pipeline, pr_url)

        # Only the idempotency-check call ran.
        assert gateway._make_request.call_count == 1

    def test_posts_when_recent_comments_do_not_contain_pr_url(self):
        """No prior comment mentions the PR URL — post a fresh comment."""
        pipeline = _make_pipeline()
        pr_url = "https://github.com/owner/repo/pull/99"

        existing = [
            {"id": "1", "body": "Some unrelated note"},
            {"id": "2", "body": "https://github.com/owner/repo/pull/42"},  # different PR
        ]

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            gateway._make_request.side_effect = [
                _comment_response(existing),
                {"ok": True},
            ]

            _writeback_pr_link_to_jira_child(pipeline, pr_url)

        assert gateway._make_request.call_count == 2
        post_call = gateway._make_request.call_args_list[1]
        assert post_call.args[0] == "/api/v1/jira/ticket/comment/add"

    def test_idempotency_scans_recent_comments_window(self):
        """The helper bounds its scan to the most recent N comments.

        Per the source docstring the walk is bounded so a chatty ticket
        does not balloon the request payload. The test instruments a
        large comment history and asserts the writeback still produces
        a single fetch + comment-add round-trip.
        """
        pipeline = _make_pipeline()
        pr_url = "https://github.com/owner/repo/pull/99"

        # 100 unrelated comments — none mention the PR URL.
        existing = [{"id": str(i), "body": f"Comment {i}"} for i in range(100)]

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            gateway._make_request.side_effect = [
                _comment_response(existing),
                {"ok": True},
            ]

            _writeback_pr_link_to_jira_child(pipeline, pr_url)

        # Two calls: idempotency fetch + comment add.
        assert gateway._make_request.call_count == 2


# ---------------------------------------------------------------------------
# Adversarial: gateway errors are logged but never raised.
# ---------------------------------------------------------------------------


class TestWritebackAdversarial:
    """Gateway 4xx / 5xx / network errors must NEVER block PR-phase completion."""

    def test_gateway_4xx_on_comments_fetch_proceeds_with_post(self):
        """An idempotency check failure is fail-open: still post the comment.

        Per the source comment: 'duplicating a single PR-link comment is
        preferable to silently dropping the writeback on a transient
        Atlassian outage.'
        """
        pipeline = _make_pipeline()
        pr_url = "https://github.com/owner/repo/pull/99"

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            # 1st call (comments fetch) raises; 2nd (post) succeeds.
            gateway._make_request.side_effect = [
                Exception("gateway 400 bad request"),
                {"ok": True},
            ]

            # MUST NOT raise.
            _writeback_pr_link_to_jira_child(pipeline, pr_url)

        # Comment add was still attempted.
        assert gateway._make_request.call_count == 2
        assert gateway._make_request.call_args_list[1].args[0] == "/api/v1/jira/ticket/comment/add"

    def test_gateway_4xx_on_comment_post_is_swallowed(self):
        """A failing comment-add does not raise out of the writeback."""
        pipeline = _make_pipeline()
        pr_url = "https://github.com/owner/repo/pull/99"

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            gateway._make_request.side_effect = [
                _comment_response([]),
                Exception("gateway 500 internal error"),
            ]

            # MUST NOT raise — Jira outage cannot block the PR phase.
            try:
                _writeback_pr_link_to_jira_child(pipeline, pr_url)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(
                    f"_writeback_pr_link_to_jira_child should swallow "
                    f"gateway errors but raised {exc!r}"
                )

    def test_malformed_comments_payload_does_not_crash(self):
        """A garbage payload (missing/None ``comments``) must not raise."""
        pipeline = _make_pipeline()
        pr_url = "https://github.com/owner/repo/pull/99"

        with patch("gateway_client.GatewayClient") as MockGateway:
            gateway = MockGateway.return_value
            # Response with no ``data`` / ``comments`` keys.
            gateway._make_request.side_effect = [{}, {"ok": True}]

            _writeback_pr_link_to_jira_child(pipeline, pr_url)

        # The 2nd call should still have fired — fail-open behaviour.
        assert gateway._make_request.call_count == 2
