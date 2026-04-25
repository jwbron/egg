"""Tests for ``egg_overseer.scrubbing`` (issue #1962).

Covers every pattern in ``_PATTERNS`` with positive AND negative cases
plus idempotency, empty-input, and ``find_secret_kinds`` parity with
``scrub_secrets``.
"""

from __future__ import annotations

import pytest
from egg_overseer.scrubbing import (
    SECRET_PATTERN_KINDS,
    find_secret_kinds,
    scrub_secrets,
)

# Matching-length sample tokens. The patterns require exactly 36
# base62 chars after the prefix or 16 uppercase alphanumerics after
# AKIA so any drift in the regex shape will fail these tests.
_GH_PAT_BODY = "A" * 36
_AWS_KEY_BODY = "B" * 16


class TestScrubGitHubPATs:
    @pytest.mark.parametrize(
        "prefix",
        ["ghp_", "ghs_", "gho_", "ghu_", "ghr_"],
    )
    def test_each_prefix_redacted(self, prefix: str) -> None:
        secret = f"{prefix}{_GH_PAT_BODY}"
        text = f"before {secret} after"
        assert scrub_secrets(text) == "before [REDACTED:gh-pat] after"

    def test_short_token_not_redacted(self) -> None:
        # Only 35 body chars — must NOT match.
        almost = "ghp_" + ("A" * 35)
        assert scrub_secrets(almost) == almost

    def test_unknown_prefix_not_redacted(self) -> None:
        # ghx_ is not in the allowlist of recognised prefixes.
        text = f"ghx_{_GH_PAT_BODY}"
        assert scrub_secrets(text) == text


class TestScrubAWSKeys:
    def test_aws_access_key_redacted(self) -> None:
        secret = f"AKIA{_AWS_KEY_BODY}"
        text = f"foo {secret} bar"
        assert scrub_secrets(text) == "foo [REDACTED:aws-key] bar"

    def test_aws_lowercase_in_body_not_redacted(self) -> None:
        # Body must be uppercase + digits only.
        text = "AKIAabcdef1234567890"
        assert scrub_secrets(text) == text

    def test_aws_short_body_not_redacted(self) -> None:
        text = f"AKIA{'A' * 15}"
        assert scrub_secrets(text) == text


class TestScrubSlackWebhook:
    def test_slack_webhook_redacted(self) -> None:
        url = "https://hooks.slack.com/services/T0/B0/abc"
        assert scrub_secrets(url) == "[REDACTED:slack-webhook]"

    def test_other_slack_url_not_redacted(self) -> None:
        url = "https://api.slack.com/methods/chat.postMessage"
        assert scrub_secrets(url) == url


class TestScrubEnvExports:
    @pytest.mark.parametrize(
        "name",
        ["GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY"],
    )
    def test_env_export_redacted(self, name: str) -> None:
        text = f"export {name}=secret123"
        assert scrub_secrets(text) == "export [REDACTED:env-export]"

    def test_env_export_with_spaces_around_equals(self) -> None:
        text = "GITHUB_TOKEN = abc"
        assert scrub_secrets(text) == "[REDACTED:env-export]"

    def test_unrelated_env_export_kept(self) -> None:
        text = "export PATH=/usr/bin"
        assert scrub_secrets(text) == text


class TestScrubGeneral:
    def test_empty_input(self) -> None:
        assert scrub_secrets("") == ""

    def test_no_secrets_returns_input_unchanged(self) -> None:
        text = "Plain markdown body with no secrets at all."
        assert scrub_secrets(text) is text or scrub_secrets(text) == text

    def test_idempotent(self) -> None:
        secret = f"ghp_{_GH_PAT_BODY}"
        once = scrub_secrets(f"x {secret} y")
        twice = scrub_secrets(once)
        assert once == twice

    def test_multiple_secrets_in_one_body(self) -> None:
        body = (
            f"Token: ghp_{_GH_PAT_BODY}\n"
            f"AWS:   AKIA{_AWS_KEY_BODY}\n"
            f"Slack: https://hooks.slack.com/services/T1/B1/xyz\n"
            f"Env:   GITHUB_TOKEN=secret"
        )
        out = scrub_secrets(body)
        assert "[REDACTED:gh-pat]" in out
        assert "[REDACTED:aws-key]" in out
        assert "[REDACTED:slack-webhook]" in out
        assert "[REDACTED:env-export]" in out
        # Original tokens must be entirely gone.
        assert _GH_PAT_BODY not in out
        assert _AWS_KEY_BODY not in out


class TestFindSecretKinds:
    def test_empty_returns_empty_list(self) -> None:
        assert find_secret_kinds("") == []

    def test_single_kind(self) -> None:
        text = f"foo ghp_{_GH_PAT_BODY} bar"
        assert find_secret_kinds(text) == ["gh-pat"]

    def test_multiple_kinds_sorted_unique(self) -> None:
        body = f"AKIA{_AWS_KEY_BODY} ghp_{_GH_PAT_BODY} ghs_{_GH_PAT_BODY} GITHUB_TOKEN=x"
        # Both ghp_ and ghs_ map to gh-pat — find_secret_kinds returns
        # the deduplicated set.
        kinds = find_secret_kinds(body)
        assert kinds == sorted(set(kinds))
        assert "aws-key" in kinds
        assert "gh-pat" in kinds
        assert "env-export" in kinds

    def test_find_secret_kinds_parity_with_scrub_secrets(self) -> None:
        # Every kind detected by find_secret_kinds is also redacted by
        # scrub_secrets (and vice versa).
        body = (
            f"ghp_{_GH_PAT_BODY} "
            f"AKIA{_AWS_KEY_BODY} "
            "https://hooks.slack.com/services/T0/B0/xyz "
            "GITHUB_TOKEN=secret"
        )
        kinds = find_secret_kinds(body)
        scrubbed = scrub_secrets(body)
        for kind in kinds:
            assert f"[REDACTED:{kind}]" in scrubbed


class TestPatternKindsTable:
    def test_pattern_kinds_table_matches_scrubber(self) -> None:
        # The predicate-only table SECRET_PATTERN_KINDS exists so the
        # gateway can reject without mutating; assert the kinds line up
        # with the redaction markers used by the scrubber.
        markers = {
            "gh-pat",
            "aws-key",
            "slack-webhook",
            "env-export",
        }
        kinds_in_table = {kind for _, kind in SECRET_PATTERN_KINDS}
        assert markers <= kinds_in_table
