"""Tests for ``orchestrator.jira_transitions`` (issue #1557, TASK-1-5).

Covers:

* :func:`_feature_flag_enabled` — environment toggle truthiness, default-off
  per risk_analyst R1 mitigation.
* :class:`OrchJiraTransitionsDisabled` raised when the feature flag is off
  and (separately) when the underlying credential loader signals
  unavailability.
* :class:`JiraTransitionsClient.transition_to_wont_do` happy path — Atlassian
  URL shape, ``{"transition": {"id": ...}}`` body, HTTP 204 success → a
  populated :class:`TransitionResult`.
* HTTP 4xx/5xx upstream → :class:`JiraTransitionFailed` with a status code
  surfaced on the exception so the Won't-Do batch (TASK-1-14) can record
  per-entry errors.
* Network-error wrapping into :class:`JiraTransitionFailed`.
* ``_client()`` lazy init under concurrent access — double-checked locking
  must keep ``self._http_client`` single-instance even when two threads race
  through :meth:`transition_to_wont_do`.
* Adversarial inputs: a project whose workflow lacks any Won't-Do-ish
  transition (``status="transition_not_found"``) and an
  already-in-state short-circuit.

All network IO is mocked via :class:`httpx.MockTransport` mirroring the
gateway-side ``test_jira_client.py`` convention; the test never touches
the real Atlassian Cloud surface.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

# orchestrator and shared paths are wired up by conftest.py
from egg_jira_credentials import JiraCredentials, JiraCredentialsUnavailable
from jira_transitions import (
    JiraTransitionFailed,
    JiraTransitionsClient,
    OrchJiraTransitionsDisabled,
    TransitionResult,
    _feature_flag_enabled,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_creds() -> JiraCredentials:
    return JiraCredentials(
        base_url="https://example.atlassian.net",
        username="alice@example.com",
        api_token="atk-xyz",
    )


@pytest.fixture
def enabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the orchestrator-direct transitions feature flag ON for the test."""
    monkeypatch.setenv("EGG_ENABLE_ORCH_JIRA_TRANSITIONS", "1")


def _make_client(
    handler,
    creds: JiraCredentials,
) -> JiraTransitionsClient:
    """Build a JiraTransitionsClient whose upstream HTTP lands in ``handler``."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return JiraTransitionsClient(
        creds_provider=lambda: creds,
        http_client=http,
    )


def _json_response(body: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# _feature_flag_enabled
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    """Defaults to OFF per risk_analyst R1; only the documented truthy
    strings enable the orchestrator-direct write path."""

    def test_unset_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EGG_ENABLE_ORCH_JIRA_TRANSITIONS", raising=False)
        assert _feature_flag_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes", "on", "ON"])
    def test_truthy_values_enable(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("EGG_ENABLE_ORCH_JIRA_TRANSITIONS", value)
        assert _feature_flag_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "  ", "2"])
    def test_falsy_values_disable(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("EGG_ENABLE_ORCH_JIRA_TRANSITIONS", value)
        assert _feature_flag_enabled() is False

    def test_whitespace_padded_truthy_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The implementation strips before lower-casing.
        monkeypatch.setenv("EGG_ENABLE_ORCH_JIRA_TRANSITIONS", "  true  ")
        assert _feature_flag_enabled() is True


# ---------------------------------------------------------------------------
# Feature-flag enforcement on the client
# ---------------------------------------------------------------------------


class TestFeatureFlagEnforcement:
    """The client itself raises ``OrchJiraTransitionsDisabled`` when the
    flag is off, regardless of whether credentials are available."""

    def test_disabled_raises_when_flag_off(
        self, monkeypatch: pytest.MonkeyPatch, fake_creds: JiraCredentials
    ) -> None:
        monkeypatch.delenv("EGG_ENABLE_ORCH_JIRA_TRANSITIONS", raising=False)

        creds_provider = MagicMock(return_value=fake_creds)
        client = JiraTransitionsClient(
            creds_provider=creds_provider,
            http_client=MagicMock(),
        )
        with pytest.raises(OrchJiraTransitionsDisabled) as exc:
            client.transition_to_wont_do("PROJ-1", "comment")
        # The error message names the flag so operators know what to flip.
        assert "EGG_ENABLE_ORCH_JIRA_TRANSITIONS" in str(exc.value)
        # v5 design (reviewer_security #14): the creds provider IS consulted
        # before the flag check so the audit log can record the principal even
        # on the disabled-flag exit path. The audit line emitted in that branch
        # uses outcome=feature_flag_disabled.
        creds_provider.assert_called_once()

    def test_credentials_unavailable_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EGG_ENABLE_ORCH_JIRA_TRANSITIONS", "true")
        creds_provider = MagicMock(side_effect=JiraCredentialsUnavailable("no creds"))
        client = JiraTransitionsClient(
            creds_provider=creds_provider,
            http_client=MagicMock(),
        )
        with pytest.raises(JiraCredentialsUnavailable):
            client.transition_to_wont_do("PROJ-1", "comment")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """End-to-end success flow: status probe → transition lookup → POST →
    re-probe — all three GETs land at the Atlassian URLs we expect, and
    the POST body is the well-known ``{"transition": {"id": "<id>"}}``."""

    def test_returns_applied_result_on_204(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            path = request.url.path
            if request.method == "GET" and path.endswith("/transitions"):
                return _json_response(
                    {
                        "transitions": [
                            {"id": "31", "name": "Won't Do"},
                            {"id": "11", "name": "In Progress"},
                        ]
                    }
                )
            if request.method == "GET":
                # Status probe — two GETs (pre + post-transition).
                # First call returns "To Do"; second returns "Won't Do".
                # Use the captured-list length to decide ordering.
                status_calls = [
                    r
                    for r in captured
                    if r.method == "GET" and not r.url.path.endswith("/transitions")
                ]
                if len(status_calls) == 1:
                    name = "To Do"
                else:
                    name = "Won't Do"
                return _json_response({"fields": {"status": {"name": name}}})
            if request.method == "POST":
                # 204 No Content per Atlassian docs.
                return httpx.Response(204)
            raise AssertionError(f"unexpected request: {request.method} {path}")

        client = _make_client(handler, fake_creds)
        result = client.transition_to_wont_do(
            "PROJ-42",
            comment="Closing per Won't-Do batch",
            epic_key="PROJ-1",
        )

        assert isinstance(result, TransitionResult)
        assert result.status == "applied"
        assert result.child_key == "PROJ-42"
        assert result.from_status == "To Do"
        assert result.to_status == "Won't Do"
        assert result.transition_id == "31"

        # Three requests: GET status, GET transitions, POST transitions, GET status.
        assert len(captured) == 4
        # The POST went to /rest/api/3/issue/PROJ-42/transitions.
        post = next(r for r in captured if r.method == "POST")
        assert post.url.path == "/rest/api/3/issue/PROJ-42/transitions"
        body = json.loads(post.content)
        assert body["transition"] == {"id": "31"}
        # Comment is appended to the body.
        assert "update" in body and "comment" in body["update"]
        # Basic-auth header is present (loader produces "Basic ...").
        assert post.headers["Authorization"].startswith("Basic ")
        assert post.headers["Accept"] == "application/json"
        assert post.headers["Content-Type"] == "application/json"

    def test_already_in_state_short_circuits(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """If the child is already Won't-Do we never POST a transition."""
        posted = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posted.append(request)
                return httpx.Response(204)
            return _json_response({"fields": {"status": {"name": "Won't Do"}}})

        client = _make_client(handler, fake_creds)
        result = client.transition_to_wont_do("PROJ-7", comment="")
        assert result.status == "already_in_state"
        assert result.from_status == "Won't Do"
        assert result.to_status == "Won't Do"
        assert result.transition_id is None
        assert posted == []  # no write issued

    def test_short_circuits_on_done_status_with_wont_do_resolution(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """v5 reviewer_code v3 #7 mitigation — the **common Atlassian
        workflow shape** has status ``Done`` (or ``Closed``) and
        resolution ``Won't Do``. The check at jira_transitions.py:245-247
        is ``current_category == "done" and resolution_lower in
        WONT_DO_NAMES``. A regression that drops the resolution-branch
        check would let the name-branch alone gate the short-circuit
        and fail to recognise this workflow as already-in-state — the
        client would re-POST the transition. This test exercises the
        resolution-branch directly, mocking the status as ``Done`` (not
        ``Won't Do``) so the name-branch never fires.
        """
        posted = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posted.append(request)
                return httpx.Response(204)
            # Atlassian-shape status payload: name="Done",
            # statusCategory.key="done", resolution.name="Won't Do".
            return _json_response(
                {
                    "fields": {
                        "status": {
                            "name": "Done",
                            "statusCategory": {"key": "done"},
                        },
                        "resolution": {"name": "Won't Do"},
                    }
                }
            )

        client = _make_client(handler, fake_creds)
        result = client.transition_to_wont_do(
            "PROJ-9",
            comment="resolved earlier",
            epic_key="PROJ-EPIC",
        )

        assert result.status == "already_in_state"
        assert result.from_status == "Done"
        assert result.to_status == "Done"
        assert result.transition_id is None
        # Regression guard: no POST issued — short-circuit fired via the
        # resolution-branch.
        assert posted == []

    def test_does_not_short_circuit_on_done_with_other_resolution(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """Defence in depth around the v5 #7 fix: a Done ticket with
        resolution ``Fixed`` (or anything else) must NOT short-circuit
        — the orchestrator's job here is to drive *to* Won't-Do.
        """
        posted = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posted.append(request)
                return httpx.Response(204)
            path = request.url.path
            if path.endswith("/transitions"):
                return _json_response({"transitions": [{"id": "31", "name": "Won't Do"}]})
            return _json_response(
                {
                    "fields": {
                        "status": {
                            "name": "Done",
                            "statusCategory": {"key": "done"},
                        },
                        "resolution": {"name": "Fixed"},
                    }
                }
            )

        client = _make_client(handler, fake_creds)
        result = client.transition_to_wont_do("PROJ-9", comment="redo")
        # POST WAS issued (no short-circuit).
        assert len(posted) == 1
        # And the result reflects an applied (or apply-attempted) outcome.
        assert result.status == "applied"

    def test_post_body_wraps_comment_in_adf_document(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """v5 reviewer_code v3 #8 mitigation — Atlassian REST API v3
        rejects raw strings for issue-comment bodies. The
        ``_wrap_text_as_adf`` helper must produce a canonical ADF doc:

            {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": <comment>}
                ]},
            ]}

        Regression guard: a refactor that drops the wrapper and
        passes ``comment_text`` directly would slip past the earlier
        ``"comment" in body["update"]`` assertion in
        ``test_returns_applied_result_on_204``. This test pins the
        exact ADF shape.
        """
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            path = request.url.path
            if request.method == "GET" and path.endswith("/transitions"):
                return _json_response({"transitions": [{"id": "31", "name": "Won't Do"}]})
            if request.method == "GET":
                return _json_response({"fields": {"status": {"name": "To Do"}}})
            if request.method == "POST":
                return httpx.Response(204)
            raise AssertionError(f"unexpected: {request.method} {path}")

        client = _make_client(handler, fake_creds)
        comment_text = "Closing per Won't-Do batch — superseded by ENG-10."
        client.transition_to_wont_do("PROJ-42", comment=comment_text)

        post = next(r for r in captured if r.method == "POST")
        body = json.loads(post.content)
        # Pin the exact ADF document shape.
        assert body["update"]["comment"] == [
            {
                "add": {
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": comment_text},
                                ],
                            },
                        ],
                    },
                },
            },
        ]

    def test_empty_comment_omits_update_block(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """Defence around v5 #8: an empty / whitespace-only comment
        must NOT produce a ``body["update"]`` block at all (no ADF
        document holding the empty string). The orchestrator either
        sends a real comment or nothing.
        """
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            path = request.url.path
            if request.method == "GET" and path.endswith("/transitions"):
                return _json_response({"transitions": [{"id": "31", "name": "Won't Do"}]})
            if request.method == "GET":
                return _json_response({"fields": {"status": {"name": "To Do"}}})
            if request.method == "POST":
                return httpx.Response(204)
            raise AssertionError(f"unexpected: {request.method} {path}")

        client = _make_client(handler, fake_creds)
        client.transition_to_wont_do("PROJ-42", comment="   ")  # whitespace only
        post = next(r for r in captured if r.method == "POST")
        body = json.loads(post.content)
        assert "update" not in body
        assert body == {"transition": {"id": "31"}}

    def test_transition_not_found_when_workflow_lacks_wont_do(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """A workflow that doesn't expose a Won't-Do-ish transition
        returns ``status='transition_not_found'`` without POSTing."""
        posted = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posted.append(request)
                return httpx.Response(204)
            path = request.url.path
            if path.endswith("/transitions"):
                return _json_response(
                    {
                        "transitions": [
                            {"id": "11", "name": "In Progress"},
                            {"id": "21", "name": "Done"},
                        ]
                    }
                )
            return _json_response({"fields": {"status": {"name": "To Do"}}})

        client = _make_client(handler, fake_creds)
        result = client.transition_to_wont_do("PROJ-9", comment="closing")
        assert result.status == "transition_not_found"
        assert result.transition_id is None
        assert result.to_status is None
        assert posted == []

    def test_special_characters_in_key_are_url_quoted(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """The child key is URL-quoted so an exotic key cannot inject path
        segments. We don't expect real Atlassian keys to contain slashes,
        but this is the contract ``quote(..., safe='')`` guarantees."""
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(str(request.url))
            if request.method == "POST":
                return httpx.Response(204)
            if request.url.path.endswith("/transitions"):
                return _json_response({"transitions": [{"id": "31", "name": "Won't Do"}]})
            return _json_response({"fields": {"status": {"name": "To Do"}}})

        client = _make_client(handler, fake_creds)
        client.transition_to_wont_do("PROJ/EVIL", comment="")
        # The slash should be %2F in every URL.
        assert all("PROJ%2FEVIL" in url for url in captured)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    """4xx/5xx and connectivity issues surface as ``JiraTransitionFailed``
    with a status code (when available) so the caller can record per-entry
    errors on the Won't-Do batch result."""

    def test_4xx_on_post_wraps_into_transition_failed(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST":
                return httpx.Response(
                    400,
                    content=b'{"errorMessages": ["Invalid transition id"]}',
                    headers={"Content-Type": "application/json"},
                )
            if path.endswith("/transitions"):
                return _json_response({"transitions": [{"id": "31", "name": "Won't Do"}]})
            return _json_response({"fields": {"status": {"name": "To Do"}}})

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraTransitionFailed) as exc:
            client.transition_to_wont_do("PROJ-1", comment="")
        assert exc.value.status_code == 400
        assert "400" in str(exc.value)

    def test_5xx_on_status_probe_wraps_into_transition_failed(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"Service unavailable")

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraTransitionFailed) as exc:
            client.transition_to_wont_do("PROJ-1", comment="")
        assert exc.value.status_code == 503

    def test_network_error_propagates_as_transport_error(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """A raw connectivity failure surfaces as an httpx transport
        exception. The client does not silently swallow it; the caller's
        ``except Exception`` boundary records the per-entry error."""

        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns failure")

        client = _make_client(handler, fake_creds)
        with pytest.raises(httpx.TransportError):
            client.transition_to_wont_do("PROJ-1", comment="")

    def test_invalid_transition_id_via_500(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """Adversarial: workflow returns an id that Atlassian later rejects.
        The POST surfaces the failure verbatim."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(500, content=b"transition invalid")
            if request.url.path.endswith("/transitions"):
                return _json_response({"transitions": [{"id": "999999", "name": "Won't Do"}]})
            return _json_response({"fields": {"status": {"name": "Open"}}})

        client = _make_client(handler, fake_creds)
        with pytest.raises(JiraTransitionFailed) as exc:
            client.transition_to_wont_do("PROJ-1", comment="")
        assert exc.value.status_code == 500


# ---------------------------------------------------------------------------
# Concurrency: double-checked locking on _client()
# ---------------------------------------------------------------------------


class TestClientLazyInit:
    """``_client`` is double-checked-locked so two threads racing through
    ``transition_to_wont_do`` cannot each build their own
    :class:`httpx.Client` and orphan one (the orphan's connection pool
    would otherwise leak until GC)."""

    def test_single_construction_under_concurrent_access(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch httpx.Client so we can count constructions cheaply.
        construct_count = 0
        real_client = MagicMock()

        def fake_ctor(*args: Any, **kwargs: Any) -> Any:
            nonlocal construct_count
            construct_count += 1
            return real_client

        monkeypatch.setattr("httpx.Client", fake_ctor)
        client = JiraTransitionsClient(creds_provider=MagicMock())

        results: list[Any] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            results.append(client._client())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one httpx.Client was built; every thread observed the
        # same instance.
        assert construct_count == 1
        assert all(r is real_client for r in results)

    def test_pre_built_http_client_short_circuits(self) -> None:
        """When the caller passes in a pre-built http_client (the
        test-injection seam), ``_client`` returns it without import or
        construction."""
        pre_built = MagicMock()
        client = JiraTransitionsClient(
            creds_provider=MagicMock(),
            http_client=pre_built,
        )
        assert client._client() is pre_built
        # Calling again is idempotent.
        assert client._client() is pre_built

    def test_invalidate_transition_cache_clears_specific_project(
        self, enabled_env: None, fake_creds: JiraCredentials
    ) -> None:
        """The transition-id cache is keyed by project; the
        :meth:`invalidate_transition_cache` escape hatch supports both
        per-project and full clears."""
        client = JiraTransitionsClient(
            creds_provider=lambda: fake_creds,
            http_client=MagicMock(),
        )
        # Manually seed the cache; we're testing the invalidation, not
        # the fetch path.
        client._transition_cache["PROJ"] = {"Won't Do": "31"}
        client._transition_cache["OTHER"] = {"Won't Do": "41"}

        client.invalidate_transition_cache("PROJ")
        assert "PROJ" not in client._transition_cache
        assert "OTHER" in client._transition_cache

        client.invalidate_transition_cache()
        assert client._transition_cache == {}
