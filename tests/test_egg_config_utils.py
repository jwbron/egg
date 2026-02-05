"""Tests for shared/egg_config/utils.py."""

from egg_config.utils import load_env_file, load_yaml_file, safe_bool, safe_int


class TestLoadEnvFile:
    """Tests for load_env_file()."""

    def test_nonexistent_file(self, tmp_path):
        result = load_env_file(tmp_path / "nonexistent.env")
        assert result == {}

    def test_empty_file(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        result = load_env_file(env_file)
        assert result == {}

    def test_simple_key_value(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\nBAZ=qux\n")
        result = load_env_file(env_file)
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_comments_ignored(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# This is a comment\nFOO=bar\n")
        result = load_env_file(env_file)
        assert result == {"FOO": "bar"}

    def test_empty_lines_ignored(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\n\n\nBAZ=qux\n")
        result = load_env_file(env_file)
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_double_quoted_values(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('FOO="bar baz"\n')
        result = load_env_file(env_file)
        assert result == {"FOO": "bar baz"}

    def test_single_quoted_values(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO='bar baz'\n")
        result = load_env_file(env_file)
        assert result == {"FOO": "bar baz"}

    def test_value_with_equals(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar=baz\n")
        result = load_env_file(env_file)
        assert result == {"FOO": "bar=baz"}

    def test_whitespace_stripped(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("  FOO  =  bar  \n")
        result = load_env_file(env_file)
        assert result == {"FOO": "bar"}

    def test_line_without_equals_ignored(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\nINVALID_LINE\nBAZ=qux\n")
        result = load_env_file(env_file)
        assert result == {"FOO": "bar", "BAZ": "qux"}


class TestLoadYamlFile:
    """Tests for load_yaml_file()."""

    def test_nonexistent_file(self, tmp_path):
        result = load_yaml_file(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_valid_yaml(self, tmp_path):
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("key: value\nnested:\n  a: 1\n  b: 2\n")
        result = load_yaml_file(yaml_file)
        assert result == {"key": "value", "nested": {"a": 1, "b": 2}}

    def test_empty_yaml(self, tmp_path):
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        result = load_yaml_file(yaml_file)
        assert result == {}

    def test_invalid_yaml(self, tmp_path):
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text(":\n  - :\n  invalid: [")
        result = load_yaml_file(yaml_file)
        assert result == {}


class TestSafeInt:
    """Tests for safe_int()."""

    def test_none_returns_default(self):
        assert safe_int(None) == 0
        assert safe_int(None, 42) == 42

    def test_valid_int_string(self):
        assert safe_int("123") == 123
        assert safe_int("-5") == -5
        assert safe_int("0") == 0

    def test_invalid_string(self):
        assert safe_int("abc") == 0
        assert safe_int("abc", 99) == 99

    def test_empty_string(self):
        assert safe_int("") == 0

    def test_float_string(self):
        assert safe_int("3.14") == 0


class TestSafeBool:
    """Tests for safe_bool()."""

    def test_none_returns_default(self):
        assert safe_bool(None) is False
        assert safe_bool(None, True) is True

    def test_true_values(self):
        assert safe_bool("true") is True
        assert safe_bool("yes") is True
        assert safe_bool("1") is True
        assert safe_bool("on") is True

    def test_false_values(self):
        assert safe_bool("false") is False
        assert safe_bool("no") is False
        assert safe_bool("0") is False
        assert safe_bool("off") is False

    def test_case_insensitive(self):
        assert safe_bool("TRUE") is True
        assert safe_bool("False") is False
        assert safe_bool("YES") is True

    def test_whitespace_stripped(self):
        assert safe_bool(" true ") is True
        assert safe_bool(" false ") is False

    def test_invalid_returns_default(self):
        assert safe_bool("maybe") is False
        assert safe_bool("maybe", True) is True
