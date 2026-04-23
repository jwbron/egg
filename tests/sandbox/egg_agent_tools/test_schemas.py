"""Tests for egg_agent_tools.schemas (argparse → JSON-schema helpers)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))

from egg_agent_tools.schemas import (  # noqa: E402
    build_tool_schema,
    derive_schema_from_argparse,
)


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="x")
    p.add_argument("--name", type=str, required=True, help="name")
    p.add_argument("--count", type=int, default=1, help="count")
    p.add_argument("--ratio", type=float, help="ratio")
    p.add_argument("--verbose", action="store_true", help="verbose")
    p.add_argument("--mode", choices=["a", "b", "c"], default="a", help="mode")
    p.add_argument("--tags", nargs="+", help="tags (required>=1)")
    p.add_argument("--notes", nargs="*", help="optional notes")
    return p


class TestDeriveSchemaFromArgparse:
    def test_required_and_properties_populated(self):
        schema = derive_schema_from_argparse(_make_parser())
        assert schema["type"] == "object"
        assert set(schema["required"]) == {"name"}
        assert "name" in schema["properties"]
        assert schema["properties"]["name"]["type"] == "string"

    def test_int_and_float_mapped(self):
        schema = derive_schema_from_argparse(_make_parser())
        assert schema["properties"]["count"]["type"] == "integer"
        assert schema["properties"]["ratio"]["type"] == "number"

    def test_store_true_maps_to_boolean(self):
        schema = derive_schema_from_argparse(_make_parser())
        assert schema["properties"]["verbose"]["type"] == "boolean"

    def test_choices_become_enum(self):
        schema = derive_schema_from_argparse(_make_parser())
        assert schema["properties"]["mode"]["enum"] == ["a", "b", "c"]

    def test_nargs_plus_becomes_array_with_min_items(self):
        schema = derive_schema_from_argparse(_make_parser())
        tags = schema["properties"]["tags"]
        assert tags["type"] == "array"
        assert tags["items"] == {"type": "string"}
        assert tags.get("minItems") == 1

    def test_nargs_star_becomes_array_without_min(self):
        schema = derive_schema_from_argparse(_make_parser())
        notes = schema["properties"]["notes"]
        assert notes["type"] == "array"
        assert notes["items"] == {"type": "string"}
        assert "minItems" not in notes

    def test_help_becomes_description(self):
        schema = derive_schema_from_argparse(_make_parser())
        assert schema["properties"]["name"]["description"] == "name"

    def test_default_preserved_for_primitive(self):
        schema = derive_schema_from_argparse(_make_parser())
        assert schema["properties"]["count"]["default"] == 1
        assert schema["properties"]["mode"]["default"] == "a"

    def test_drop_excludes_names(self):
        schema = derive_schema_from_argparse(_make_parser(), drop={"verbose"})
        assert "verbose" not in schema["properties"]

    def test_help_and_func_actions_skipped(self):
        p = _make_parser()
        # Simulate a subparser with a set_defaults(func=...) call.
        p.set_defaults(func=lambda args: 0)
        schema = derive_schema_from_argparse(p)
        assert "help" not in schema["properties"]
        assert "func" not in schema["properties"]

    def test_no_required_key_when_nothing_required(self):
        p = argparse.ArgumentParser()
        p.add_argument("--foo", type=str)
        schema = derive_schema_from_argparse(p)
        assert "required" not in schema


class TestBuildToolSchema:
    def test_override_property_replaces_definition(self):
        base = derive_schema_from_argparse(_make_parser())
        override = {"properties": {"name": {"type": "string", "minLength": 5}}}
        merged = build_tool_schema(base, overrides=override)
        assert merged["properties"]["name"] == {"type": "string", "minLength": 5}

    def test_override_required_replaces_list(self):
        base = derive_schema_from_argparse(_make_parser())
        merged = build_tool_schema(base, overrides={"required": ["count"]})
        assert merged["required"] == ["count"]

    def test_untouched_fields_pass_through(self):
        base = derive_schema_from_argparse(_make_parser())
        merged = build_tool_schema(base, overrides={"properties": {"name": {"type": "string"}}})
        assert merged["properties"]["count"]["type"] == "integer"

    def test_none_base_and_no_overrides_returns_empty_object(self):
        merged = build_tool_schema()
        assert merged == {"type": "object", "properties": {}}

    def test_top_level_description_merges(self):
        merged = build_tool_schema(
            {"type": "object", "properties": {}}, overrides={"description": "x"}
        )
        assert merged["description"] == "x"

    def test_overrides_do_not_mutate_base(self):
        base = derive_schema_from_argparse(_make_parser())
        snapshot = base["properties"]["name"].copy()
        build_tool_schema(base, overrides={"properties": {"name": {"type": "boolean"}}})
        assert base["properties"]["name"] == snapshot

    def test_additional_properties_false_passes_through(self):
        merged = build_tool_schema(
            {"type": "object", "properties": {}},
            overrides={"additionalProperties": False},
        )
        assert merged["additionalProperties"] is False
