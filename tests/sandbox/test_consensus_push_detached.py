"""Tests for the detached-HEAD fallback in ``consensus_push`` (#2200).

When ``git branch --show-current`` returns empty (detached HEAD), the
helper used to bail out with ``"could not determine current branch
for push"``.  That trapped BRC producer agents that ended up detached
after a rebase.  The fix falls back to ``git rev-parse HEAD`` and
sends a ``commit_sha`` field; the gateway derives the refspec from
the session's assigned branch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent.parent
for _p in (
    str(_PROJECT_ROOT / "sandbox"),
    str(_PROJECT_ROOT / "shared"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from egg_agent_tools.push import consensus_push  # noqa: E402


class _FakeResponse:
    """Minimal urlopen response stand-in that supports the context manager protocol."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _check_output_factory(branch_value: str, head_sha: str = "deadbeef" * 5):
    """Build a check_output stub that returns ``branch_value`` for --show-current.

    Other git invocations (rev-parse HEAD, config branch.X.merge) are
    served from a small lookup table.  ``CalledProcessError`` is raised
    for unmatched commands so the test fails loudly rather than silently.
    """

    import subprocess as _subprocess

    def stub(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "branch", "--show-current"]:
            return f"{branch_value}\n"
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return f"{head_sha}\n"
        if cmd[:2] == ["git", "config"] and cmd[2].startswith("branch."):
            # No tracking config — let the helper fall back to plain branch.
            raise _subprocess.CalledProcessError(1, cmd)
        raise _subprocess.CalledProcessError(1, cmd)

    return stub


@pytest.fixture
def gateway_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://gateway.test")
    monkeypatch.setenv("EGG_SESSION_TOKEN", "test-token")
    monkeypatch.setenv("CONTAINER_ID", "issue-2200-coder")
    monkeypatch.setenv("EGG_REPO_PATH", "/repo")
    # No EGG_BRANCH set — we want the helper to use its non-retarget path
    # so the test exercises the SHA fallback cleanly.
    monkeypatch.delenv("EGG_BRANCH", raising=False)


class TestConsensusPushDetachedHead:
    def test_detached_head_falls_back_to_commit_sha(self, gateway_env):
        """Empty branch + valid HEAD sends commit_sha in the payload (no refspec)."""
        captured: list[bytes] = []

        def fake_urlopen(req, timeout=120):
            captured.append(req.data)
            return _FakeResponse({"data": {"stdout": "ok\n", "stderr": ""}})

        with (
            patch(
                "egg_agent_tools.push.subprocess.check_output",
                side_effect=_check_output_factory(branch_value="", head_sha="cafef00d" * 5),
            ),
            patch("egg_agent_tools.push.urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            rc, err = consensus_push()

        assert rc == 0, err
        assert len(captured) == 1
        payload = json.loads(captured[0])
        assert payload.get("consensus_push") is True
        assert payload.get("commit_sha") == "cafef00d" * 5
        # The helper must NOT send a refspec when on detached HEAD — that
        # is the gateway's job (it derives <sha>:refs/heads/<assigned>
        # from the session) and sending both would invite drift.
        assert "refspec" not in payload

    def test_detached_head_no_sha_returns_error(self, gateway_env):
        """If both branch and HEAD lookups fail, the helper returns an error."""

        def stub(cmd, *args, **kwargs):
            import subprocess as _subprocess

            raise _subprocess.CalledProcessError(1, cmd)

        with (
            patch("egg_agent_tools.push.subprocess.check_output", side_effect=stub),
        ):
            rc, err = consensus_push()
        assert rc == 1
        assert err is not None
        assert "HEAD" in err

    def test_attached_head_still_sends_refspec(self, gateway_env):
        """Attached-HEAD path is unchanged: refspec built client-side, no commit_sha."""
        captured: list[bytes] = []

        def fake_urlopen(req, timeout=120):
            captured.append(req.data)
            return _FakeResponse({"data": {"stdout": "ok\n", "stderr": ""}})

        with (
            patch(
                "egg_agent_tools.push.subprocess.check_output",
                side_effect=_check_output_factory(branch_value="egg/issue-2200"),
            ),
            patch("egg_agent_tools.push.urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            rc, err = consensus_push()

        assert rc == 0, err
        payload = json.loads(captured[0])
        assert "refspec" in payload
        assert "commit_sha" not in payload
