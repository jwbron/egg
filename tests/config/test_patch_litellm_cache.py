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
    """A missing target file must raise SystemExit from ``_apply``.

    ``_patch_root`` installs ``NEW_MODULES`` before it runs ``PATCHES``, so an
    empty root aborts in ``_install_module``'s "destination package missing"
    branch and never reaches ``_apply`` at all — the assertion would pass while
    testing something else entirely. Create the module destinations (but none
    of the patch targets) so the failure under test is the one named."""
    for spec in plc.NEW_MODULES:
        (tmp_path / spec["dest"]).parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(SystemExit) as excinfo:
        plc._patch_root(str(tmp_path))
    assert "file not found" in str(excinfo.value), "aborted before reaching _apply"


def test_new_module_refuses_to_clobber_a_foreign_file(tmp_path):
    """Every other operation in this script is fail-loud on drift; so is this.

    The guard must read the file *on disk*, not our own ``NEW_MODULES``
    literal: keying it on the ``_egg_`` prefix of a hardcoded destination could
    only ever fire if someone edited this script, which is a lint of its own
    constants and not upstream-drift detection. A real file at a real
    destination, lacking egg's provenance header, must abort the build."""
    _build_fixture_root(tmp_path)
    spec = plc.NEW_MODULES[0]
    dest = tmp_path / spec["dest"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("# upstream took this name\n")

    # An unrecognised file at our own destination aborts — no synthetic spec
    # needed, which is the point: this path is reachable in production.
    with pytest.raises(SystemExit) as excinfo:
        plc._install_module(str(tmp_path), spec)
    assert "refusing to overwrite" in str(excinfo.value)
    assert dest.read_text() == "# upstream took this name\n", "clobbered anyway"


def test_new_module_overwrites_a_stale_egg_install(tmp_path):
    """A previous image layer's copy carries the header, so it is ours to
    replace — the guard must distinguish stale-ours from foreign."""
    _build_fixture_root(tmp_path)
    spec = plc.NEW_MODULES[0]
    dest = tmp_path / spec["dest"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(plc.EGG_MODULE_HEADER + "# an older revision of this module\n")

    plc._install_module(str(tmp_path), spec)
    assert "an older revision" not in dest.read_text()
    assert dest.read_text().startswith(plc.EGG_MODULE_HEADER)


def test_new_module_destinations_carry_the_egg_prefix(tmp_path):
    """The prefix keeps upstream from ever taking one of our paths. Enforced at
    install time so a future ``NEW_MODULES`` entry cannot quietly drop it."""
    for spec in plc.NEW_MODULES:
        assert Path(spec["dest"]).name.startswith(plc.EGG_MODULE_PREFIX), spec["label"]

    _build_fixture_root(tmp_path)
    unprefixed = dict(plc.NEW_MODULES[0], dest="llms/openrouter/capabilities.py")
    with pytest.raises(SystemExit) as excinfo:
        plc._install_module(str(tmp_path), unprefixed)
    assert "must be prefixed" in str(excinfo.value)


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

    Patch 7's gate imports ``litellm.llms.openrouter._egg_capabilities``, so if
    the module install silently no-ops the patched gate raises ImportError on
    every request — caught by its ``except Exception``, which would put us right
    back at the silent-drop behaviour the patch exists to remove."""
    _build_fixture_root(tmp_path)
    plc._patch_root(str(tmp_path))

    for spec in plc.NEW_MODULES:
        dest = tmp_path / spec["dest"]
        assert dest.is_file(), f"{spec['label']}: not installed"
        source = Path(plc._module_source(spec["source"], spec["label"]))
        installed = dest.read_text()
        assert installed.startswith(plc.EGG_MODULE_HEADER), f"{spec['label']}: no provenance"
        assert installed == plc.EGG_MODULE_HEADER + source.read_text(), (
            f"{spec['label']}: content drift"
        )


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

    # The stock gate, verbatim from 1.86.2, following the needle. Applying the
    # patch to this and asserting on the RESULT tests the invariant the
    # docstring claims; substring checks against the replacement string alone
    # would pass on a patch that deleted the branch entirely.
    stock_gate = (
        "            if litellm.supports_reasoning(\n"
        '                model=model, custom_llm_provider="openrouter"\n'
        "            ) or litellm.supports_reasoning(model=model):\n"
        '                supported_params.append("reasoning_effort")\n'
        '                supported_params.append("thinking")\n'
        "        except Exception:\n"
        "            pass\n"
        "        return list(dict.fromkeys(supported_params))\n"
    )
    target = tmp_path / patch7["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch7["needle"] + stock_gate)

    plc._apply(
        str(target),
        present=patch7["present"],
        needle=patch7["needle"],
        replacement=patch7["replacement"],
        label=patch7["label"],
    )
    result = target.read_text()

    # The live lookup is consulted...
    assert "_egg_capabilities" in result
    assert '"reasoning_effort" in _advertised' in result
    # ...and the stock model-map branch is still there, untouched, downstream
    # of it: the live answer can ADD a knob, never withhold one.
    assert stock_gate in result, "patch 7 must not replace the stock model-map branch"
    # The inserted block precedes it and does not return out of the function.
    inserted = result[: result.index(stock_gate)]
    assert "supported_params.append" in inserted
    assert "\n            return" not in inserted, "patch 7 must not short-circuit the stock branch"
    # Failures in the lookup must never propagate into a request.
    assert "except Exception:" in inserted
    # Only reasoning_effort is admitted. OpenRouter's `reasoning` field is a
    # different wire shape from Anthropic's `thinking`, not a spelling of it.
    assert '"thinking"' not in inserted


# The tail of ``_translate_thinking_to_openai`` as it stands in 1.86.2,
# verbatim, from the Claude branch through the assignments. Patch 9's needle is
# a slice of this; keeping the surrounding lines lets the test assert what the
# gate sits between, which is the whole invariant.
_STOCK_THINKING_TAIL_HEAD = (
    '        model = new_kwargs.get("model", "")\n'
    "        if self.is_anthropic_claude_model(model):\n"
    '            new_kwargs["thinking"] = thinking  # type: ignore\n'
    "            return\n"
    "\n"
    "        reasoning_effort = self.translate_anthropic_thinking_to_reasoning_effort(\n"
    "            cast(Dict[str, Any], thinking)\n"
    "        )\n"
    "        if not reasoning_effort:\n"
    "            return\n"
    "\n"
)
_STOCK_THINKING_TAIL_FOOT = (
    "        auto_summary = is_reasoning_auto_summary_enabled()\n"
    "        if summary:\n"
    '            new_kwargs["reasoning_effort"] = cast(\n'
    "                Any,\n"
    "                {\n"
    '                    "effort": reasoning_effort,\n'
    '                    "summary": summary,\n'
    "                },\n"
    "            )\n"
    "        elif auto_summary:\n"
    '            new_kwargs["reasoning_effort"] = cast(\n'
    "                Any,\n"
    "                {\n"
    '                    "effort": reasoning_effort,\n'
    '                    "summary": "detailed",\n'
    "                },\n"
    "            )\n"
    "        else:\n"
    '            new_kwargs["reasoning_effort"] = reasoning_effort\n'
)


def test_patch9_gates_synthesis_without_touching_the_claude_branch(tmp_path):
    """Patch 9 must stop the adapter manufacturing a ``reasoning_effort``.

    On ``/v1/messages`` litellm derives ``reasoning_effort`` from the caller's
    ``thinking`` budget for every non-Claude model. That derived value is a cap
    BELOW the model default (#3624: kimi-k3 means 3130 reasoning tokens with no
    param vs 340 with ``high``), so Patch 7 alone would silently shallow every
    agent turn. The Claude branch, which forwards ``thinking`` unchanged, must
    be unaffected — and so must the assignments, which the gate returns before
    rather than rewriting."""
    patch9 = next(p for p in plc.PATCHES if p["label"].startswith("Patch 9/"))

    target = tmp_path / patch9["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _STOCK_THINKING_TAIL_HEAD + patch9["needle"] + _STOCK_THINKING_TAIL_FOOT,
    )

    plc._apply(
        str(target),
        present=patch9["present"],
        needle=patch9["needle"],
        replacement=patch9["replacement"],
        label=patch9["label"],
    )
    result = target.read_text()

    assert _STOCK_THINKING_TAIL_HEAD in result, "patch 9 must leave the Claude path alone"
    assert _STOCK_THINKING_TAIL_FOOT in result, "patch 9 must not rewrite the assignments"

    # The gate sits after the derivation and returns (rather than falling
    # through) when synthesis is off, so nothing is assigned.
    gate = result[result.index(patch9["present"]) : result.index(_STOCK_THINKING_TAIL_FOOT)]
    assert "_egg_anthropic_thinking_policy" in gate
    assert "if not _egg_synthesize:\n                return\n" in gate
    # A missing policy module must fall back to the policy's OWN default (off),
    # not to stock behaviour — otherwise the failure mode is the regression.
    assert "_egg_synthesize = False" in gate


def test_patch9_does_not_suppress_an_explicitly_requested_effort(tmp_path):
    """The gate's scope is the *derived* bucket, not the whole function.

    Stock reaches the assignment two ways: from ``budget_tokens`` (a ceiling
    nobody asked for) or from ``output_config.effort`` on an adaptive request,
    which is the caller saying outright what they want. Suppressing the second
    would be discarding an instruction, not declining to invent one — a
    different change from the one the patch documents."""
    patch9 = next(p for p in plc.PATCHES if p["label"].startswith("Patch 9/"))

    target = tmp_path / patch9["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _STOCK_THINKING_TAIL_HEAD + patch9["needle"] + _STOCK_THINKING_TAIL_FOOT,
    )
    plc._apply(
        str(target),
        present=patch9["present"],
        needle=patch9["needle"],
        replacement=patch9["replacement"],
        label=patch9["label"],
    )
    result = target.read_text()

    # The adaptive override still runs, and it is what exempts the request.
    override = '                reasoning_effort = output_config["effort"]\n'
    assert override in result, "the adaptive override must survive the patch"
    assert result.index(override) < result.index(patch9["present"]), (
        "the gate must sit after the override, not before it"
    )
    assert f"{override}                _egg_effort_is_explicit = True\n" in result
    assert "if not _egg_effort_is_explicit:\n" in result


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
