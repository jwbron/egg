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
    # Matched on "Patch 4/" rather than the full label so adding a patch (and
    # renumbering the denominators) does not silently turn this into a
    # StopIteration instead of a real assertion.
    patch4 = next(p for p in plc.PATCHES if p["label"].startswith("Patch 4/"))

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


def test_new_modules_are_installed_into_each_root(tmp_path):
    """``NEW_MODULES`` drops whole files that have no stock counterpart.

    Patch 7's gate imports ``litellm.llms.openrouter.capabilities``, so if the
    module install silently no-ops the patched gate raises ImportError on every
    request — caught by its ``except Exception``, which would put us right back
    at the silent-drop behaviour the patch exists to remove."""
    _build_fixture_root(tmp_path)
    plc._patch_root(str(tmp_path))

    for spec in plc.NEW_MODULES:
        dest = tmp_path / spec["dest"]
        assert dest.is_file(), f"{spec['label']}: not installed"
        source = Path(plc._module_source(spec["source"], spec["label"]))
        assert dest.read_text() == source.read_text(), f"{spec['label']}: content drift"


def test_new_module_install_is_idempotent(tmp_path):
    _build_fixture_root(tmp_path)
    plc._patch_root(str(tmp_path))
    first = {spec["dest"]: (tmp_path / spec["dest"]).read_text() for spec in plc.NEW_MODULES}
    plc._patch_root(str(tmp_path))
    for dest, content in first.items():
        assert (tmp_path / dest).read_text() == content


def test_missing_staged_module_fails_loud():
    """A missing staged file must abort the build, not skip the install."""
    with pytest.raises(SystemExit):
        plc._module_source("definitely-not-a-real-module.py", "test label")


def test_patch7_gate_is_additive_not_substitutive(tmp_path):
    """Patch 7 must UNION the live answer with the stock model-map answer.

    OpenRouter's ``supported_parameters`` under-reports ``reasoning_effort``
    (deepseek-r1 advertises only ``reasoning``), so letting live data win
    outright would drop a knob the map correctly allows — trading one silent
    drop for another. The stock ``supports_reasoning`` branch must therefore
    survive the patch."""
    patch7 = next(p for p in plc.PATCHES if p["label"].startswith("Patch 7/"))
    replacement = patch7["replacement"]

    # The live lookup is consulted...
    assert "capabilities" in replacement
    assert '"reasoning_effort" in _advertised' in replacement
    # ...and the stock gate is still reached afterwards, with no early return
    # between them that would make the live answer authoritative.
    assert replacement.rstrip().endswith("try:")
    assert "return" not in replacement, "patch 7 must not short-circuit the stock model-map branch"
    # Failures in the lookup must never propagate into a request.
    assert "except Exception:" in replacement


def test_patch8_needle_disambiguates_the_two_drop_sites(tmp_path):
    """litellm 1.86.2 has TWO ``drop_params`` branches in utils.py.

    They share the identical ``if litellm.drop_params is True or (...)``
    condition; only what follows differs (a bare ``pass`` in the embeddings
    path, the pop loop in ``get_optional_params``). Matching on the shared
    condition would patch whichever came first — the same needle-uniqueness
    trap as Patch 4. A fixture carrying the sibling ``pass`` form first must be
    left untouched."""
    patch8 = next(p for p in plc.PATCHES if p["label"].startswith("Patch 8/"))

    sibling = (
        "SENTINEL_PASS_SITE_BEGIN\n"
        "            if litellm.drop_params is True or (\n"
        "                drop_params is not None and drop_params is True\n"
        "            ):\n"
        "                pass\n"
        "SENTINEL_PASS_SITE_END\n"
    )
    fixture = sibling + "\n" + patch8["needle"]

    target = tmp_path / patch8["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fixture)

    plc._apply(
        str(target),
        present=patch8["present"],
        needle=patch8["needle"],
        replacement=patch8["replacement"],
        label=patch8["label"],
    )
    result = target.read_text()

    assert result.count(patch8["present"]) == 1
    start = result.index("SENTINEL_PASS_SITE_BEGIN")
    end = result.index("SENTINEL_PASS_SITE_END") + len("SENTINEL_PASS_SITE_END\n")
    assert result[start:end] == sibling, "patch 8 rewrote the embeddings-path drop site"
