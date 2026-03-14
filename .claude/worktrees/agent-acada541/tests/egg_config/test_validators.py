"""
Tests for egg_config.validators module.
"""

from egg_config.validators import (
    mask_secret,
    validate_anthropic_key,
    validate_email,
    validate_github_token,
    validate_non_empty,
    validate_port,
    validate_url,
)


class TestValidateUrl:
    """Tests for validate_url function."""

    def test_valid_https_url(self):
        """Test valid HTTPS URL passes."""
        is_valid, error = validate_url("https://example.com")
        assert is_valid
        assert error is None

    def test_valid_https_with_path(self):
        """Test HTTPS URL with path passes."""
        is_valid, error = validate_url("https://example.com/api/v1/users")
        assert is_valid
        assert error is None

    def test_valid_https_with_port(self):
        """Test HTTPS URL with port passes."""
        is_valid, error = validate_url("https://example.com:8443/api")
        assert is_valid
        assert error is None

    def test_http_fails_by_default(self):
        """Test HTTP URL fails when require_https=True (default)."""
        is_valid, error = validate_url("http://example.com")
        assert not is_valid
        assert "HTTPS" in error

    def test_http_allowed_when_require_https_false(self):
        """Test HTTP URL passes when require_https=False."""
        is_valid, error = validate_url("http://example.com", require_https=False)
        assert is_valid
        assert error is None

    def test_empty_url(self):
        """Test empty URL fails."""
        is_valid, error = validate_url("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_missing_scheme(self):
        """Test URL without scheme fails."""
        is_valid, error = validate_url("example.com")
        assert not is_valid
        assert "scheme" in error.lower()

    def test_missing_host(self):
        """Test URL without host fails."""
        is_valid, error = validate_url("https://")
        assert not is_valid
        assert "host" in error.lower()

    def test_invalid_scheme(self):
        """Test URL with invalid scheme fails."""
        is_valid, error = validate_url("ftp://example.com")
        assert not is_valid
        assert "http" in error.lower()


class TestValidateEmail:
    """Tests for validate_email function."""

    def test_valid_email(self):
        """Test valid email passes."""
        is_valid, error = validate_email("user@example.com")
        assert is_valid
        assert error is None

    def test_valid_email_with_plus(self):
        """Test email with plus sign passes."""
        is_valid, error = validate_email("user+tag@example.com")
        assert is_valid
        assert error is None

    def test_valid_email_with_dots(self):
        """Test email with dots in local part passes."""
        is_valid, error = validate_email("first.last@example.com")
        assert is_valid
        assert error is None

    def test_valid_email_subdomain(self):
        """Test email with subdomain passes."""
        is_valid, error = validate_email("user@mail.example.com")
        assert is_valid
        assert error is None

    def test_empty_email(self):
        """Test empty email fails."""
        is_valid, error = validate_email("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_no_at_sign(self):
        """Test email without @ fails."""
        is_valid, error = validate_email("userexample.com")
        assert not is_valid
        assert "format" in error.lower()

    def test_no_domain(self):
        """Test email without domain fails."""
        is_valid, _error = validate_email("user@")
        assert not is_valid

    def test_no_tld(self):
        """Test email without TLD fails."""
        is_valid, _error = validate_email("user@example")
        assert not is_valid


class TestValidateGithubToken:
    """Tests for validate_github_token function."""

    def test_valid_personal_token(self):
        """Test valid personal access token (ghp_) passes."""
        is_valid, error = validate_github_token("ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert is_valid
        assert error is None

    def test_valid_new_format_pat(self):
        """Test valid new format PAT (github_pat_) passes."""
        is_valid, error = validate_github_token(
            "github_pat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        )
        assert is_valid
        assert error is None

    def test_valid_app_token(self):
        """Test valid app installation token (ghs_) passes."""
        is_valid, error = validate_github_token("ghs_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert is_valid
        assert error is None

    def test_valid_oauth_token(self):
        """Test valid OAuth token (gho_) passes."""
        is_valid, error = validate_github_token("gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert is_valid
        assert error is None

    def test_valid_user_to_server_token(self):
        """Test valid user-to-server token (ghu_) passes."""
        is_valid, error = validate_github_token("ghu_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert is_valid
        assert error is None

    def test_empty_token(self):
        """Test empty token fails."""
        is_valid, error = validate_github_token("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_invalid_prefix(self):
        """Test token with wrong prefix fails."""
        is_valid, error = validate_github_token("invalid_token_here")
        assert not is_valid
        assert "ghp_" in error or "github_pat_" in error

    def test_too_short(self):
        """Test token that is too short fails."""
        is_valid, error = validate_github_token("ghp_short")
        assert not is_valid
        assert "short" in error.lower()


class TestValidateAnthropicKey:
    """Tests for validate_anthropic_key function."""

    def test_valid_key(self):
        """Test valid Anthropic key passes."""
        is_valid, error = validate_anthropic_key(
            "sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        )
        assert is_valid
        assert error is None

    def test_empty_key(self):
        """Test empty key fails."""
        is_valid, error = validate_anthropic_key("")
        assert not is_valid
        assert "empty" in error.lower()

    def test_invalid_prefix(self):
        """Test key with wrong prefix fails."""
        is_valid, error = validate_anthropic_key("sk-openai-xxxx")
        assert not is_valid
        assert "sk-ant-" in error

    def test_too_short(self):
        """Test key that is too short fails."""
        is_valid, error = validate_anthropic_key("sk-ant-short")
        assert not is_valid
        assert "short" in error.lower()


class TestMaskSecret:
    """Tests for mask_secret function."""

    def test_masks_long_secret(self):
        """Test masking a long secret shows prefix."""
        masked = mask_secret("super-secret-token-12345")
        assert masked == "supe********************"
        assert masked.startswith("supe")
        assert "*" in masked

    def test_masks_slack_token(self):
        """Test masking a Slack token preserves prefix."""
        masked = mask_secret("xoxb-1234567890-abcdefghij")
        assert masked.startswith("xoxb")

    def test_custom_visible_chars(self):
        """Test custom number of visible characters."""
        masked = mask_secret("secret", visible_chars=2)
        assert masked == "se****"

    def test_empty_value(self):
        """Test empty value returns [EMPTY]."""
        masked = mask_secret("")
        assert masked == "[EMPTY]"

    def test_none_value(self):
        """Test None value returns [EMPTY]."""
        masked = mask_secret(None)
        assert masked == "[EMPTY]"

    def test_short_value(self):
        """Test value shorter than visible_chars is fully masked."""
        masked = mask_secret("abc", visible_chars=4)
        assert masked == "***"

    def test_exact_length(self):
        """Test value equal to visible_chars is fully masked."""
        masked = mask_secret("abcd", visible_chars=4)
        assert masked == "****"


class TestValidateNonEmpty:
    """Tests for validate_non_empty function."""

    def test_valid_value(self):
        """Test non-empty value passes."""
        is_valid, error = validate_non_empty("value", "field")
        assert is_valid
        assert error is None

    def test_empty_string(self):
        """Test empty string fails."""
        is_valid, error = validate_non_empty("", "my_field")
        assert not is_valid
        assert "my_field" in error
        assert "empty" in error.lower()

    def test_whitespace_only(self):
        """Test whitespace-only string fails."""
        is_valid, error = validate_non_empty("   ", "token")
        assert not is_valid
        assert "empty" in error.lower()

    def test_none_value(self):
        """Test None value fails."""
        is_valid, error = validate_non_empty(None, "api_key")
        assert not is_valid
        assert "api_key" in error
        assert "not set" in error.lower()


class TestValidatePort:
    """Tests for validate_port function."""

    def test_valid_port_int(self):
        """Test valid port as integer passes."""
        is_valid, error = validate_port(8080)
        assert is_valid
        assert error is None

    def test_valid_port_string(self):
        """Test valid port as string passes."""
        is_valid, error = validate_port("443")
        assert is_valid
        assert error is None

    def test_min_port(self):
        """Test minimum valid port (1) passes."""
        is_valid, _error = validate_port(1)
        assert is_valid

    def test_max_port(self):
        """Test maximum valid port (65535) passes."""
        is_valid, _error = validate_port(65535)
        assert is_valid

    def test_port_zero(self):
        """Test port 0 fails."""
        is_valid, error = validate_port(0)
        assert not is_valid
        assert "between 1 and 65535" in error

    def test_port_too_high(self):
        """Test port > 65535 fails."""
        is_valid, error = validate_port(70000)
        assert not is_valid
        assert "between 1 and 65535" in error

    def test_negative_port(self):
        """Test negative port fails."""
        is_valid, _error = validate_port(-1)
        assert not is_valid

    def test_invalid_string(self):
        """Test non-numeric string fails."""
        is_valid, error = validate_port("abc")
        assert not is_valid
        assert "number" in error.lower()

    def test_none_port(self):
        """Test None port fails."""
        is_valid, _error = validate_port(None)
        assert not is_valid
