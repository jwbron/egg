"""Tests for gateway/commit_registry_client.py (issue #1882, TASK-5-4).

Covers:

- Successful register (200)
- Idempotent register returning 200 (treated as success)
- 409 (authorship collision) — treated as benign success
- Non-2xx response — logged and reported as failure
- Network timeout / URLError — fail-closed, False result
- Bulk register happy path + failure surface
- Bulk lookup happy path, network-fail fail-closed (empty dict), malformed body
- Missing/empty lifecycle secret — no auth header added
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

_gateway_path = Path(__file__).parent.parent
if str(_gateway_path) not in sys.path:
    sys.path.insert(0, str(_gateway_path))


@pytest.fixture
def client(monkeypatch):
    from commit_registry_client import (  # type: ignore[import-not-found]
        CommitRegistryClient,
        reset_client,
    )

    monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator.test:9849")
    monkeypatch.setenv("EGG_LIFECYCLE_SECRET", "test-secret")
    reset_client()
    return CommitRegistryClient(timeout=1.0, lookup_timeout=1.0)


_VALID_SHA = "a" * 40
_OTHER_SHA = "b" * 40


# ---------------------------------------------------------------------------
# register (single)
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_200_returns_true(self, client, monkeypatch):
        calls: list = []

        def fake_post(path, payload, *, timeout):
            calls.append((path, payload, timeout))
            return 200, {"success": True, "sha": payload["sha"]}, None

        import commit_registry_client as mod

        monkeypatch.setattr(mod, "_post", fake_post)
        assert (
            client.register(
                sha=_VALID_SHA,
                role="coder",
                pipeline_id="issue-1882",
                repo="owner/repo",
                branch="egg/issue-1882",
            )
            is True
        )
        assert len(calls) == 1
        path, payload, _timeout = calls[0]
        assert path == "/api/v1/commit-authorship/register"
        assert payload["sha"] == _VALID_SHA
        assert payload["role"] == "coder"
        assert payload["pipeline_id"] == "issue-1882"
        assert payload["repo"] == "owner/repo"
        assert payload["branch"] == "egg/issue-1882"

    def test_register_409_is_benign_success(self, client, monkeypatch):
        """Collision at the store level is still a "durably decided" result."""
        import commit_registry_client as mod

        def fake_post(*_a, **_kw):
            return 409, {"success": False, "existing_role": "tester"}, "HTTPError 409"

        monkeypatch.setattr(mod, "_post", fake_post)
        assert client.register(sha=_VALID_SHA, role="coder", pipeline_id="issue-1882") is True

    def test_register_500_returns_false(self, client, monkeypatch):
        import commit_registry_client as mod

        monkeypatch.setattr(mod, "_post", lambda *_a, **_kw: (500, None, "HTTPError 500"))
        assert client.register(sha=_VALID_SHA, role="coder", pipeline_id="issue-1882") is False

    def test_register_network_error_returns_false(self, client, monkeypatch):
        import commit_registry_client as mod

        monkeypatch.setattr(mod, "_post", lambda *_a, **_kw: (0, None, "Network error"))
        assert client.register(sha=_VALID_SHA, role="coder", pipeline_id="issue-1882") is False

    def test_register_401_returns_false(self, client, monkeypatch):
        """A 401 surfaces as False (misconfigured secret is an operator bug)."""
        import commit_registry_client as mod

        monkeypatch.setattr(mod, "_post", lambda *_a, **_kw: (401, None, "HTTPError 401"))
        assert client.register(sha=_VALID_SHA, role="coder", pipeline_id="issue-1882") is False


# ---------------------------------------------------------------------------
# register_bulk
# ---------------------------------------------------------------------------


class TestRegisterBulk:
    def test_bulk_happy_path(self, client, monkeypatch):
        import commit_registry_client as mod

        calls: list = []

        def fake_post(path, payload, *, timeout):
            calls.append((path, payload, timeout))
            return 200, {"success": True, "results": []}, None

        monkeypatch.setattr(mod, "_post", fake_post)
        items = [
            {
                "sha": _VALID_SHA,
                "role": "coder",
                "pipeline_id": "issue-1882",
                "repo": None,
                "branch": None,
            },
            {
                "sha": _OTHER_SHA,
                "role": "coder",
                "pipeline_id": "issue-1882",
                "repo": None,
                "branch": None,
            },
        ]
        assert client.register_bulk(items) is True
        assert calls[0][0] == "/api/v1/commit-authorship/register-bulk"
        assert calls[0][1]["items"] == items

    def test_bulk_empty_returns_true_with_no_call(self, client, monkeypatch):
        import commit_registry_client as mod

        calls: list = []
        monkeypatch.setattr(mod, "_post", lambda *_a, **_kw: calls.append("posted"))
        assert client.register_bulk([]) is True
        assert calls == []

    def test_bulk_500_returns_false(self, client, monkeypatch):
        import commit_registry_client as mod

        monkeypatch.setattr(mod, "_post", lambda *_a, **_kw: (500, None, "HTTPError 500"))
        items = [
            {
                "sha": _VALID_SHA,
                "role": "coder",
                "pipeline_id": "issue-1882",
                "repo": None,
                "branch": None,
            }
        ]
        assert client.register_bulk(items) is False


# ---------------------------------------------------------------------------
# lookup_bulk
# ---------------------------------------------------------------------------


class TestLookupBulk:
    def test_lookup_happy_path(self, client, monkeypatch):
        import commit_registry_client as mod

        monkeypatch.setattr(
            mod,
            "_post",
            lambda *_a, **_kw: (
                200,
                {
                    "success": True,
                    "attribution": {
                        _VALID_SHA: "coder",
                        _OTHER_SHA: None,
                    },
                },
                None,
            ),
        )
        result = client.lookup_bulk([_VALID_SHA, _OTHER_SHA])
        assert result == {_VALID_SHA: "coder", _OTHER_SHA: None}

    def test_lookup_empty_returns_empty_no_call(self, client, monkeypatch):
        import commit_registry_client as mod

        calls: list = []
        monkeypatch.setattr(mod, "_post", lambda *_a, **_kw: calls.append("posted"))
        assert client.lookup_bulk([]) == {}
        assert calls == []

    def test_lookup_failure_returns_empty(self, client, monkeypatch):
        """Fail-closed: network failure -> empty dict; caller treats as unregistered."""
        import commit_registry_client as mod

        monkeypatch.setattr(mod, "_post", lambda *_a, **_kw: (0, None, "Network error"))
        assert client.lookup_bulk([_VALID_SHA]) == {}

    def test_lookup_malformed_body_returns_empty(self, client, monkeypatch):
        import commit_registry_client as mod

        monkeypatch.setattr(
            mod,
            "_post",
            lambda *_a, **_kw: (200, {"success": True, "attribution": "not a dict"}, None),
        )
        assert client.lookup_bulk([_VALID_SHA]) == {}

    def test_lookup_drops_alien_keys(self, client, monkeypatch):
        """Extra SHAs the server might emit are stripped from the result."""
        import commit_registry_client as mod

        monkeypatch.setattr(
            mod,
            "_post",
            lambda *_a, **_kw: (
                200,
                {
                    "success": True,
                    "attribution": {
                        _VALID_SHA: "coder",
                        "alien-sha": "hacker",
                    },
                },
                None,
            ),
        )
        # Only _VALID_SHA was requested — alien-sha must not appear.
        result = client.lookup_bulk([_VALID_SHA])
        assert result == {_VALID_SHA: "coder"}

    def test_lookup_missing_attribution_key_maps_requested_shas_to_none(self, client, monkeypatch):
        """Server omitted ``attribution`` → every requested SHA -> None."""
        import commit_registry_client as mod

        monkeypatch.setattr(mod, "_post", lambda *_a, **_kw: (200, {"success": True}, None))
        # Missing attribution dict is treated as empty-by-default; every
        # requested sha maps to None (unregistered) rather than getting
        # dropped — the caller still sees the fail-closed signal.
        assert client.lookup_bulk([_VALID_SHA]) == {_VALID_SHA: None}


# ---------------------------------------------------------------------------
# _post — low-level HTTP layer
# ---------------------------------------------------------------------------


class TestPostLayer:
    def test_post_sends_auth_header_when_secret_present(self, monkeypatch):
        import commit_registry_client as mod

        seen_request: list = []

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                pass

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            seen_request.append(req)
            return _FakeResp()

        monkeypatch.setenv("EGG_LIFECYCLE_SECRET", "super-secret")
        monkeypatch.setattr(mod, "urlopen", fake_urlopen)

        status, body, err = mod._post(
            "/api/v1/commit-authorship/register",
            {"sha": _VALID_SHA, "role": "coder"},
            timeout=1.0,
        )
        assert status == 200
        assert body == {"ok": True}
        assert err is None
        req = seen_request[0]
        headers = {k.lower(): v for k, v in req.header_items()}
        assert headers.get("authorization") == "Bearer super-secret"
        assert headers.get("content-type") == "application/json"
        assert headers.get("x-egg-source") == "gateway"

    def test_post_omits_auth_header_when_no_secret(self, monkeypatch):
        import commit_registry_client as mod

        seen_request: list = []

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                pass

            def read(self):
                return b""

        def fake_urlopen(req, timeout=None):
            seen_request.append(req)
            return _FakeResp()

        monkeypatch.delenv("EGG_LIFECYCLE_SECRET", raising=False)
        monkeypatch.setattr(mod, "urlopen", fake_urlopen)
        mod._post("/api/v1/commit-authorship/register", {}, timeout=1.0)
        req = seen_request[0]
        headers = {k.lower(): v for k, v in req.header_items()}
        assert "authorization" not in headers

    def test_post_returns_status_0_on_url_error(self, monkeypatch):
        import commit_registry_client as mod

        def fake_urlopen(*_a, **_kw):
            raise URLError("connection refused")

        monkeypatch.setattr(mod, "urlopen", fake_urlopen)
        status, body, err = mod._post("/x", {}, timeout=1.0)
        assert status == 0
        assert body is None
        assert err and "Network error" in err

    def test_post_returns_status_0_on_timeout(self, monkeypatch):
        import commit_registry_client as mod

        def fake_urlopen(*_a, **_kw):
            raise TimeoutError("slow")

        monkeypatch.setattr(mod, "urlopen", fake_urlopen)
        status, body, err = mod._post("/x", {}, timeout=0.001)
        assert status == 0
        assert err and "Network error" in err

    def test_post_returns_http_error_status(self, monkeypatch):
        import io

        import commit_registry_client as mod

        def fake_urlopen(*_a, **_kw):
            raise HTTPError(
                url="http://x",
                code=409,
                msg="Conflict",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"existing_role": "tester"}'),
            )

        monkeypatch.setattr(mod, "urlopen", fake_urlopen)
        status, body, err = mod._post("/x", {}, timeout=1.0)
        assert status == 409
        assert body == {"existing_role": "tester"}
        assert err == "HTTPError 409"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_client_returns_same_instance(self):
        from commit_registry_client import (  # type: ignore[import-not-found]
            get_client,
            reset_client,
        )

        reset_client()
        a = get_client()
        b = get_client()
        assert a is b

    def test_reset_client_clears_singleton(self):
        from commit_registry_client import (  # type: ignore[import-not-found]
            get_client,
            reset_client,
        )

        reset_client()
        a = get_client()
        reset_client()
        b = get_client()
        assert a is not b
