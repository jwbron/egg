"""Integration tests for credential isolation and session security.

Verifies that sandbox containers cannot access gateway secrets,
sessions are bound to IP addresses, and tokens have sufficient entropy.
"""

import time

import pytest

from integration_tests.conftest import exec_in_container


@pytest.mark.integration
@pytest.mark.security
class TestCredentialIsolation:
    """Tests that sandbox containers cannot access gateway credentials."""

    def test_container_cannot_read_secrets(self, egg_stack, isolated_container):
        """Container on the test network cannot access /secrets/ in the gateway.

        Secrets are mounted only in the gateway container, not in sandbox
        containers. This test verifies the isolation.
        """
        # Try to read /secrets/ from within the test container
        returncode, stdout, stderr = exec_in_container(
            isolated_container.container_id,
            ["ls", "/secrets/"],
            timeout=5,
        )
        # /secrets/ should not exist in the alpine container
        assert returncode != 0, (
            f"Test container should not have /secrets/ directory. Contents: {stdout}"
        )

    def test_container_env_has_no_github_token(self, egg_stack, isolated_container):
        """Container does not have GITHUB_TOKEN in its environment."""
        returncode, stdout, _ = exec_in_container(
            isolated_container.container_id,
            ["env"],
            timeout=5,
        )
        assert returncode == 0
        env_vars = stdout.upper()
        assert "GITHUB_TOKEN" not in env_vars, (
            "SECURITY VIOLATION: GITHUB_TOKEN found in container environment"
        )
        assert "GITHUB_USER_TOKEN" not in env_vars, (
            "SECURITY VIOLATION: GITHUB_USER_TOKEN found in container environment"
        )
        assert "BOT_GITHUB_TOKEN" not in env_vars, (
            "SECURITY VIOLATION: BOT_GITHUB_TOKEN found in container environment"
        )

    def test_container_cannot_reach_github_directly(self, egg_stack, isolated_container):
        """Container on the isolated network cannot reach github.com directly.

        GitHub access must go through the gateway sidecar. Direct access
        would bypass policy enforcement.
        """
        returncode, stdout, stderr = exec_in_container(
            isolated_container.container_id,
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "--connect-timeout",
                "5",
                "https://github.com",
            ],
            timeout=15,
        )
        # Should fail -- either connection refused, timeout, or DNS failure
        if returncode == 0 and stdout == "200":
            pytest.fail("SECURITY VIOLATION: Isolated container reached github.com directly")


@pytest.mark.integration
@pytest.mark.security
class TestSessionSecurity:
    """Tests for session security properties."""

    def test_deleted_session_rejected(self, egg_stack):
        """A deleted session token is rejected for subsequent requests."""
        container_id = f"test-deleted-{time.time_ns()}"
        result = egg_stack.create_session(container_id=container_id)
        token = result.get("data", result).get("session_token")
        assert token

        # Delete the session
        egg_stack.delete_session(token)

        # Attempt to use the deleted token
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        assert resp.status_code == 401, (
            f"Deleted session token should be rejected, got {resp.status_code}"
        )

    def test_tokens_have_sufficient_entropy(self, egg_stack):
        """Session tokens have high entropy (not guessable)."""
        tokens = []
        for i in range(3):
            container_id = f"test-entropy-{i}-{time.time_ns()}"
            result = egg_stack.create_session(container_id=container_id)
            token = result.get("data", result).get("session_token")
            if token:
                tokens.append(token)

        # Cleanup
        for token in tokens:
            egg_stack.delete_session(token)

        assert len(tokens) >= 2, "Need at least 2 tokens for entropy check"

        # All tokens should be unique
        assert len(set(tokens)) == len(tokens), (
            "Session tokens are not unique -- potential entropy problem"
        )

        # Tokens should be long enough (at least 32 characters for 256-bit security)
        for token in tokens:
            assert len(token) >= 32, f"Token too short ({len(token)} chars) -- insufficient entropy"

    def test_session_not_rejected_on_source_ip_mismatch(self, egg_stack):
        """Under k3s the gateway no longer rejects on source-IP mismatch.

        Pre-k3s, sessions were bound to the requesting container's IP
        and any request from a different IP was rejected with 401.
        That check was removed when the runtime moved to k8s
        (`gateway/auth.py`): pod IPs are ephemeral and rotate on
        restart, so binding sessions to them produced false-positive
        auth failures on every benign pod recycle.  The session
        token itself is now the sole credential — `source_ip` is
        kept on the request for audit logging only.

        This test guards that documented relaxation: a token used
        from a "different" IP than the one declared at session
        creation must NOT come back 401.  (Anything other than 401
        from the auth layer is acceptable — the request may still
        4xx for unrelated reasons like a missing repo or 500 if the
        downstream handler hits an internal error, both of which
        are out of scope for this auth-only assertion.)
        """
        container_id = f"test-ip-bind-{time.time_ns()}"
        result = egg_stack.create_session(
            container_id=container_id,
            container_ip="172.40.0.50",
        )
        token = result.get("data", result).get("session_token")
        assert token

        resp = egg_stack.api_request(
            "POST",
            "/api/v1/git/execute",
            token=token,
            json_data={
                "repo_path": "/home/egg/repos/test-repo",
                "operation": "status",
            },
        )
        assert resp.status_code != 401, (
            f"Source-IP mismatch should NOT reject under the post-k3s "
            f"auth model (see gateway/auth.py:120), but got 401: "
            f"{resp.text[:300]}"
        )

        egg_stack.delete_session(token)


@pytest.mark.integration
@pytest.mark.security
class TestPathTraversal:
    """Tests for path traversal attacks on various endpoints."""

    TRAVERSAL_PAYLOADS = [
        "../../etc/passwd",
        "../../../etc/shadow",
        "/etc/passwd",
        "%2e%2e/%2e%2e/etc/passwd",
        "..\\..\\etc\\passwd",
        "/home/egg/repos/test-repo/../../../etc/passwd",
    ]

    def test_git_execute_path_traversal(self, egg_stack, gateway_session):
        """Path traversal in git/execute repo_path is blocked."""
        token = gateway_session.get("session_token")
        for payload in self.TRAVERSAL_PAYLOADS:
            resp = egg_stack.api_request(
                "POST",
                "/api/v1/git/execute",
                token=token,
                json_data={
                    "repo_path": payload,
                    "operation": "status",
                },
            )
            assert resp.status_code in (400, 403), (
                f"Path traversal not blocked for git/execute with "
                f"payload '{payload}': status={resp.status_code}"
            )

    def test_git_push_path_traversal(self, egg_stack, gateway_session):
        """Path traversal in git/push repo_path is blocked."""
        token = gateway_session.get("session_token")
        for payload in self.TRAVERSAL_PAYLOADS:
            resp = egg_stack.api_request(
                "POST",
                "/api/v1/git/push",
                token=token,
                json_data={
                    "repo_path": payload,
                    "remote": "origin",
                    "refspec": "HEAD:egg-test",
                },
            )
            assert resp.status_code in (400, 403), (
                f"Path traversal not blocked for git/push with "
                f"payload '{payload}': status={resp.status_code}"
            )

    def test_gh_execute_path_traversal(self, egg_stack, gateway_session):
        """Path traversal in gh/execute args is handled safely."""
        token = gateway_session.get("session_token")
        resp = egg_stack.api_request(
            "POST",
            "/api/v1/gh/execute",
            token=token,
            json_data={
                "args": ["api", "../../etc/passwd"],
            },
        )
        # Should not expose file contents even if the command is processed
        if resp.status_code == 200:
            body = resp.json()
            output = str(body.get("data", {}).get("output", ""))
            assert "root:" not in output, (
                "SECURITY VIOLATION: Path traversal exposed /etc/passwd contents"
            )
