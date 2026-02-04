"""
Tests for the setup.py module validators.

These tests focus on the validation helper methods used during setup:
- _validate_choice: validates value is one of valid choices
- _validate_prefix: validates value starts with expected prefix
- _validate_first_char: validates first character is valid
"""

from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Load setup module (use egg_setup to avoid conflict with pytest's setup fixture)
setup_path = Path(__file__).parent.parent.parent / "setup.py"
loader = SourceFileLoader("egg_setup", str(setup_path))
egg_setup = loader.load_module()

MinimalSetup = egg_setup.MinimalSetup


@pytest.fixture
def setup_instance():
    """Create a MinimalSetup instance with mocked dependencies."""
    instance = MinimalSetup.__new__(MinimalSetup)
    instance.logger = MagicMock()
    instance.prompter = MagicMock()
    instance.update_mode = False
    return instance


class TestValidateChoice:
    """Tests for _validate_choice validator."""

    def test_valid_choice_passes(self, setup_instance):
        """Test that valid choice does not raise."""
        setup_instance._validate_choice("1", ["1", "2"], "Choose 1 or 2")

    def test_invalid_choice_raises(self, setup_instance):
        """Test that invalid choice raises ValueError."""
        with pytest.raises(ValueError, match="Choose 1 or 2"):
            setup_instance._validate_choice("3", ["1", "2"], "Choose 1 or 2")

    def test_case_insensitive_valid(self, setup_instance):
        """Test case insensitive matching passes."""
        setup_instance._validate_choice(
            "API_KEY", ["api_key", "oauth"], "Must be api_key or oauth", case_insensitive=True
        )

    def test_case_insensitive_invalid(self, setup_instance):
        """Test case insensitive matching still rejects invalid."""
        with pytest.raises(ValueError, match="Must be api_key or oauth"):
            setup_instance._validate_choice(
                "invalid", ["api_key", "oauth"], "Must be api_key or oauth", case_insensitive=True
            )

    def test_case_sensitive_rejects_wrong_case(self, setup_instance):
        """Test case sensitive matching rejects wrong case."""
        with pytest.raises(ValueError, match="Choose 1 or 2"):
            setup_instance._validate_choice("A", ["a", "b"], "Choose 1 or 2")


class TestValidatePrefix:
    """Tests for _validate_prefix validator."""

    def test_valid_prefix_passes(self, setup_instance):
        """Test that valid prefix does not raise."""
        setup_instance._validate_prefix("ghp_abc123", "ghp_", "Token must start with 'ghp_'")

    def test_invalid_prefix_raises(self, setup_instance):
        """Test that invalid prefix raises ValueError."""
        with pytest.raises(ValueError, match="Token must start with 'ghp_'"):
            setup_instance._validate_prefix("invalid", "ghp_", "Token must start with 'ghp_'")

    def test_empty_allowed_passes(self, setup_instance):
        """Test that empty value passes when allow_empty=True."""
        setup_instance._validate_prefix(
            "", "ghp_", "Token must start with 'ghp_'", allow_empty=True
        )

    def test_empty_not_allowed_raises(self, setup_instance):
        """Test that empty value raises when allow_empty=False."""
        with pytest.raises(ValueError, match="Token must start with 'ghp_'"):
            setup_instance._validate_prefix("", "ghp_", "Token must start with 'ghp_'")

    def test_slack_user_id_prefix(self, setup_instance):
        """Test Slack user ID validation (starts with U)."""
        setup_instance._validate_prefix("U12345", "U", "User ID must start with U")

    def test_slack_user_id_invalid(self, setup_instance):
        """Test invalid Slack user ID raises."""
        with pytest.raises(ValueError, match="User ID must start with U"):
            setup_instance._validate_prefix("12345", "U", "User ID must start with U")


class TestValidateFirstChar:
    """Tests for _validate_first_char validator."""

    def test_valid_first_char_passes(self, setup_instance):
        """Test that valid first char does not raise."""
        setup_instance._validate_first_char(
            "C12345", ["D", "C", "G"], "Channel ID must start with D, C, or G"
        )

    def test_all_valid_chars_pass(self, setup_instance):
        """Test all valid first chars pass."""
        for prefix in ["D", "C", "G"]:
            setup_instance._validate_first_char(
                f"{prefix}12345", ["D", "C", "G"], "Channel ID must start with D, C, or G"
            )

    def test_invalid_first_char_raises(self, setup_instance):
        """Test that invalid first char raises ValueError."""
        with pytest.raises(ValueError, match="Channel ID must start with D, C, or G"):
            setup_instance._validate_first_char(
                "X12345", ["D", "C", "G"], "Channel ID must start with D, C, or G"
            )

    def test_empty_allowed_passes(self, setup_instance):
        """Test that empty value passes when allow_empty=True."""
        setup_instance._validate_first_char(
            "", ["D", "C", "G"], "Channel ID must start with D, C, or G", allow_empty=True
        )

    def test_empty_not_allowed_raises(self, setup_instance):
        """Test that empty value raises when allow_empty=False."""
        with pytest.raises(ValueError, match="Channel ID must start with D, C, or G"):
            setup_instance._validate_first_char(
                "", ["D", "C", "G"], "Channel ID must start with D, C, or G"
            )
