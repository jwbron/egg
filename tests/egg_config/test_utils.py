"""Tests for shared egg_config utils module."""

from egg_config.utils import load_env_file, load_yaml_file, safe_bool, safe_int


class TestLoadEnvFile:
    """Tests for load_env_file function."""

    def test_simple_env_file(self, tmp_path):
        """Parse simple KEY=VALUE pairs."""
        env_file = tmp_path / ".env"
        env_file.write_text("KEY1=value1\nKEY2=value2\n")
        result = load_env_file(env_file)
        assert result["KEY1"] == "value1"
        assert result["KEY2"] == "value2"

    def test_skip_comments(self, tmp_path):
        """Skip comment lines."""
        env_file = tmp_path / ".env"
        env_file.write_text("# This is a comment\nKEY=value\n")
        result = load_env_file(env_file)
        assert len(result) == 1
        assert result["KEY"] == "value"

    def test_skip_empty_lines(self, tmp_path):
        """Skip empty lines."""
        env_file = tmp_path / ".env"
        env_file.write_text("\n\nKEY=value\n\n")
        result = load_env_file(env_file)
        assert len(result) == 1

    def test_quoted_values_double(self, tmp_path):
        """Strip double quotes from values."""
        env_file = tmp_path / ".env"
        env_file.write_text('KEY="quoted value"\n')
        result = load_env_file(env_file)
        assert result["KEY"] == "quoted value"

    def test_quoted_values_single(self, tmp_path):
        """Strip single quotes from values."""
        env_file = tmp_path / ".env"
        env_file.write_text("KEY='single quoted'\n")
        result = load_env_file(env_file)
        assert result["KEY"] == "single quoted"

    def test_nonexistent_file(self, tmp_path):
        """Return empty dict for nonexistent file."""
        result = load_env_file(tmp_path / "nonexistent.env")
        assert result == {}

    def test_empty_file(self, tmp_path):
        """Return empty dict for empty file."""
        env_file = tmp_path / ".env"
        env_file.write_text("")
        result = load_env_file(env_file)
        assert result == {}

    def test_whitespace_handling(self, tmp_path):
        """Handle whitespace in keys and values."""
        env_file = tmp_path / ".env"
        env_file.write_text("  KEY  =  value  \n")
        result = load_env_file(env_file)
        assert result["KEY"] == "value"

    def test_value_with_equals(self, tmp_path):
        """Handle values containing equals signs."""
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=val=ue\n")
        result = load_env_file(env_file)
        assert result["KEY"] == "val=ue"

    def test_line_without_equals(self, tmp_path):
        """Skip lines without equals sign."""
        env_file = tmp_path / ".env"
        env_file.write_text("not_a_pair\nKEY=value\n")
        result = load_env_file(env_file)
        assert len(result) == 1
        assert result["KEY"] == "value"


class TestLoadYamlFile:
    """Tests for load_yaml_file function."""

    def test_simple_yaml(self, tmp_path):
        """Parse simple YAML file."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("key1: value1\nkey2: value2\n")
        result = load_yaml_file(yaml_file)
        assert result["key1"] == "value1"
        assert result["key2"] == "value2"

    def test_nested_yaml(self, tmp_path):
        """Parse nested YAML."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("parent:\n  child: value\n")
        result = load_yaml_file(yaml_file)
        assert result["parent"]["child"] == "value"

    def test_nonexistent_file(self, tmp_path):
        """Return empty dict for nonexistent file."""
        result = load_yaml_file(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_empty_yaml(self, tmp_path):
        """Return empty dict for empty YAML file."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("")
        result = load_yaml_file(yaml_file)
        assert result == {}

    def test_invalid_yaml(self, tmp_path):
        """Return empty dict for invalid YAML."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("{{invalid: yaml: content}}")
        result = load_yaml_file(yaml_file)
        assert result == {}

    def test_yaml_with_list(self, tmp_path):
        """Parse YAML with list values."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("items:\n  - one\n  - two\n")
        result = load_yaml_file(yaml_file)
        assert result["items"] == ["one", "two"]


class TestSafeInt:
    """Tests for safe_int function."""

    def test_valid_integer(self):
        """Parse valid integer string."""
        assert safe_int("42") == 42

    def test_negative_integer(self):
        """Parse negative integer."""
        assert safe_int("-10") == -10

    def test_zero(self):
        """Parse zero."""
        assert safe_int("0") == 0

    def test_none_returns_default(self):
        """None returns default."""
        assert safe_int(None) == 0
        assert safe_int(None, default=99) == 99

    def test_invalid_returns_default(self):
        """Invalid string returns default."""
        assert safe_int("not_a_number") == 0
        assert safe_int("abc", default=5) == 5

    def test_empty_returns_default(self):
        """Empty string returns default."""
        assert safe_int("", default=10) == 10

    def test_float_string_returns_default(self):
        """Float string returns default (can't parse as int)."""
        assert safe_int("3.14") == 0

    def test_custom_default(self):
        """Custom default value."""
        assert safe_int(None, default=100) == 100


class TestSafeBool:
    """Tests for safe_bool function."""

    def test_true_values(self):
        """All true-like values."""
        assert safe_bool("true") is True
        assert safe_bool("True") is True
        assert safe_bool("TRUE") is True
        assert safe_bool("yes") is True
        assert safe_bool("YES") is True
        assert safe_bool("1") is True
        assert safe_bool("on") is True

    def test_false_values(self):
        """All false-like values."""
        assert safe_bool("false") is False
        assert safe_bool("False") is False
        assert safe_bool("FALSE") is False
        assert safe_bool("no") is False
        assert safe_bool("NO") is False
        assert safe_bool("0") is False
        assert safe_bool("off") is False

    def test_none_returns_default(self):
        """None returns default."""
        assert safe_bool(None) is False
        assert safe_bool(None, default=True) is True

    def test_invalid_returns_default(self):
        """Invalid string returns default."""
        assert safe_bool("maybe") is False
        assert safe_bool("maybe", default=True) is True

    def test_whitespace_handling(self):
        """Whitespace is stripped."""
        assert safe_bool("  true  ") is True
        assert safe_bool("  false  ") is False

    def test_empty_string_returns_default(self):
        """Empty string returns default."""
        assert safe_bool("") is False
        assert safe_bool("", default=True) is True
