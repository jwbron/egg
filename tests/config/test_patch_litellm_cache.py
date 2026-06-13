"""Unit tests for the egg-litellm build-time patch script.

``config/litellm/patch_litellm_cache.py`` edits the installed ``litellm``
package in place at image-build time (see PR #3190 / #3199). The real
``litellm==1.86.2`` is not a project dependency — and cannot run on the
repo's Python — so we exercise the patch *mechanics* against fixtures that
embed the exact needles the script targets, then assert the resulting
source. This is the patch-script regression the CI image build doesn't give
us: the build catches version drift via the fail-loud needle check, but
nothing else exercises idempotency, the replacement payload, or the Patch 4
needle-uniqueness anchor.

The fixtures are derived from ``PATCHES`` itself (the module-level spec
list), so there is no fixture/needle drift: a needle change automatically
flows into the fixture the test patches.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "litellm"


def _load_patch_module():
    """Load ``patch_litellm_cache`` from disk (it isn't an importable package)."""
    path = CONFIG_DIR / "patch_litellm_cache.py"
    spec = importlib.util.spec_from_file_location("patch_litellm_cache", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["patch_litellm_cache"] = module
    spec.loader.exec_module(module)
    return module


plc = _load_patch_module()


def _build_fixture_root(root: Path) -> None:
    """Write one fixture file per patched ``litellm`` path, each containing
    every needle that targets that path concatenated verbatim."""
    by_file: dict[str, list[str]] = {}
    for spec in plc.PATCHES:
        by_file.setdefault(spec["file"], []).append(spec["needle"])
    for rel, needles in by_file.items():
        fpath = root / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text("# fixture head\n\n" + "\n\n".join(needles) + "\n\n# fixture tail\n")


def test_each_needle_occurs_once_in_its_fixture(tmp_path):
    """Sanity: the needles are distinct, so the fixture contains each exactly
    once. If this fails the other assertions can't be trusted."""
    _build_fixture_root(tmp_path)
    for spec in plc.PATCHES:
        src = (tmp_path / spec["file"]).read_text()
        assert src.count(spec["needle"]) == 1, spec["label"]


def test_patch_applies_all_markers(tmp_path):
    """After ``_patch_root`` every patch's present-marker is in its file and
    the raw needle has been rewritten away."""
    _build_fixture_root(tmp_path)
    plc._patch_root(str(tmp_path))

    for spec in plc.PATCHES:
        src = (tmp_path / spec["file"]).read_text()
        assert spec["present"] in src, f"{spec['label']}: marker missing after patch"
        # The replacement is now present; raw needle should be gone unless the
        # needle is (intentionally) a substring of the replacement.
        assert spec["replacement"] in src, f"{spec['label']}: replacement missing"


def test_patch_is_idempotent(tmp_path):
    """Re-running over already-patched source is a no-op (no error, no double
    application) — each ``_apply`` short-circuits on its present-marker."""
    _build_fixture_root(tmp_path)
    plc._patch_root(str(tmp_path))
    first = {spec["file"]: (tmp_path / spec["file"]).read_text() for spec in plc.PATCHES}

    plc._patch_root(str(tmp_path))  # second pass must change nothing
    for rel, content in first.items():
        assert (tmp_path / rel).read_text() == content, f"{rel}: not idempotent"
        # Marker count unchanged → no double-apply.
    for spec in plc.PATCHES:
        src = (tmp_path / spec["file"]).read_text()
        assert src.count(spec["present"]) == 1, f"{spec['label']}: applied twice"


def test_missing_needle_fails_loud(tmp_path):
    """A drifted source (needle absent, marker absent) must raise SystemExit
    rather than silently shipping an unpatched image."""
    _build_fixture_root(tmp_path)
    # Corrupt one fixture so a needle no longer matches.
    target = plc.PATCHES[0]
    fpath = tmp_path / target["file"]
    fpath.write_text("# drifted source with no matching needle\n")

    with pytest.raises(SystemExit):
        plc._patch_root(str(tmp_path))


def test_missing_file_fails_loud(tmp_path):
    """A missing target file must raise SystemExit, not pass silently."""
    # Empty root — none of the litellm paths exist.
    with pytest.raises(SystemExit):
        plc._patch_root(str(tmp_path))


def test_patch4_needle_anchors_on_content_block_function(tmp_path):
    """Regression for the Patch 4 needle-uniqueness fix (#3199 review).

    The bare ``thinking_blocks`` elif appears in two sibling functions. The
    Patch 4 needle anchors on the preceding text-block elif
    (``choice.delta.content is not None ...``), which is unique to
    ``_translate_streaming_openai_chunk_to_anthropic_content_block``. A
    fixture containing a *second* (sibling-function-style) bare
    ``thinking_blocks`` elif — preceded by a ``tool_calls`` block, not the
    text elif — must be left untouched."""
    patch4 = next(p for p in plc.PATCHES if p["label"].startswith("Patch 4/5"))

    # Sibling function: bare thinking_blocks elif preceded by a tool_calls
    # branch (mirrors _translate_streaming_openai_chunk_to_anthropic). It must
    # NOT match the Patch 4 needle.
    sibling = (
        "SENTINEL_SIBLING_BEGIN\n"
        "            if choice.delta.tool_calls is not None:\n"
        "                partial_json = ''\n"
        "            elif isinstance(choice, StreamingChoices) and hasattr(\n"
        '                choice.delta, "thinking_blocks"\n'
        "            ):\n"
        "                pass\n"
        "SENTINEL_SIBLING_END\n"
    )
    fixture = sibling + "\n" + patch4["needle"]

    f2 = tmp_path / patch4["file"]
    f2.parent.mkdir(parents=True, exist_ok=True)
    f2.write_text(fixture)

    plc._apply(
        str(f2),
        present=patch4["present"],
        needle=patch4["needle"],
        replacement=patch4["replacement"],
        label=patch4["label"],
    )
    result = f2.read_text()

    # The intended branch was rewritten exactly once.
    assert result.count(patch4["present"]) == 1
    # The sibling block is byte-for-byte unchanged.
    start = result.index("SENTINEL_SIBLING_BEGIN")
    end = result.index("SENTINEL_SIBLING_END") + len("SENTINEL_SIBLING_END\n")
    assert result[start:end] == sibling
