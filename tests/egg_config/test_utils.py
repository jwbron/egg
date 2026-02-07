"""Tests for egg_config/utils.py - Utility functions for config loading."""


from egg_config.utils import load_env_file, load_yaml_file, safe_bool, safe_int


class TestLoadEnvFile:
    """Tests for load_env_file."""

    def test_basic_key_value(self, tmp_path):
        """Reads basic KEY=VALUE pairs."""
        env_file = tmp_path / ".env"
        env_file.write_text("KEY1=value1\nKEY2=value2\n")
        result = load_env_file(env_file)
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_skips_comments(self, tmp_path):
        """Skips comment lines."""
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\nKEY=value\n")
        result = load_env_file(env_file)
        assert result == {"KEY": "value"}

    def test_skips_empty_lines(self, tmp_path):
        """Skips empty lines."""
        env_file = tmp_path / ".env"
        env_file.write_text("\n\nKEY=value\n\n")
        result = load_env_file(env_file)
        assert result == {"KEY": "value"}

    def test_strips_quotes(self, tmp_path):
        """Strips quotes from values."""
        env_file = tmp_path / ".env"
        env_file.write_text('KEY1="quoted"\nKEY2=\'single\'\n')
        result = load_env_file(env_file)
        assert result["KEY1"] == "quoted"
        assert result["KEY2"] == "single"

    def test_nonexistent_file(self, tmp_path):
        """Returns empty dict for nonexistent file."""
        result = load_env_file(tmp_path / "nonexistent")
        assert result == {}

    def test_handles_equals_in_value(self, tmp_path):
        """Handles values containing = signs."""
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=val=ue\n")
        result = load_env_file(env_file)
        assert result == {"KEY": "val=ue"}


class TestLoadYamlFile:
    """Tests for load_yaml_file."""

    def test_basic_yaml(self, tmp_path):
        """Reads basic YAML file."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("key: value\nnested:\n  inner: data\n")
        result = load_yaml_file(yaml_file)
        assert result["key"] == "value"
        assert result["nested"]["inner"] == "data"

    def test_nonexistent_file(self, tmp_path):
        """Returns empty dict for nonexistent file."""
        result = load_yaml_file(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_empty_yaml(self, tmp_path):
        """Returns empty dict for empty YAML file."""
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        result = load_yaml_file(yaml_file)
        assert result == {}

    def test_invalid_yaml(self, tmp_path):
        """Returns empty dict for invalid YAML."""
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text("{{bad yaml: [")
        result = load_yaml_file(yaml_file)
        assert result == {}


class TestSafeInt:
    """Tests for safe_int."""

    def test_valid_integer(self):
        """Parses valid integer string."""
        assert safe_int("42") == 42

    def test_none_returns_default(self):
        """Returns default for None."""
        assert safe_int(None) == 0
        assert safe_int(None, 10) == 10

    def test_invalid_string(self):
        """Returns default for invalid string."""
        assert safe_int("not-a-number") == 0

    def test_empty_string(self):
        """Returns default for empty string."""
        assert safe_int("") == 0

    def test_custom_default(self):
        """Uses custom default value."""
        assert safe_int("bad", 99) == 99


class TestSafeBool:
    """Tests for safe_bool."""

    def test_true_values(self):
        """Recognizes true values."""
        assert safe_bool("true") is True
        assert safe_bool("yes") is True
        assert safe_bool("1") is True
        assert safe_bool("on") is True
        assert safe_bool("TRUE") is True
        assert safe_bool("Yes") is True

    def test_false_values(self):
        """Recognizes false values."""
        assert safe_bool("false") is False
        assert safe_bool("no") is False
        assert safe_bool("0") is False
        assert safe_bool("off") is False

    def test_none_returns_default(self):
        """Returns default for None."""
        assert safe_bool(None) is False
        assert safe_bool(None, True) is True

    def test_invalid_returns_default(self):
        """Returns default for unrecognized values."""
        assert safe_bool("maybe") is False
        assert safe_bool("maybe", True) is True

    def test_strips_whitespace(self):
        """Strips whitespace from value."""
        assert safe_bool("  true  ") is True
