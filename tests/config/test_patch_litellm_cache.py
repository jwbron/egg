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


def _patch_by_description(description: str) -> dict:
    """Find a patch spec by the descriptive tail of its label.

    Deliberately NOT by number. Numbers are positional and get reused: the
    1.94.0 bump (#3697) retired four patches and renumbered the rest, and a
    lookup keyed on ``"Patch 4/"`` silently re-bound to a completely different
    patch and kept passing — testing nothing, while reading green. The
    description is the stable identity, and an exact-count assertion turns a
    retired patch into a loud failure instead of a false pass.
    """
    matches = [p for p in plc.PATCHES if p["label"].endswith(f"({description})")]
    assert len(matches) == 1, (
        f"expected exactly one patch described {description!r}, found "
        f"{[p['label'] for p in matches]}"
    )
    return matches[0]


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


def test_malformed_replacement_fails_at_build_time(tmp_path):
    """A replacement with wrong indentation applies *cleanly*.

    The needle check only proves we found the right spot, not that what we put
    there is valid Python. Without this guard the broken result passes the
    build, ships in the image, and surfaces as a pod CrashLoopBackOff when
    litellm imports the file — the one place nothing is watching."""
    path = tmp_path / "victim.py"
    stock = "def f():\n    return 1\n"
    path.write_text(stock)

    with pytest.raises(SystemExit) as excinfo:
        plc._apply(
            str(path),
            present="# egg marker",
            needle="    return 1\n",
            replacement="# egg marker\nreturn 1\n",
            label="Patch X",
        )
    assert "does not parse" in str(excinfo.value)
    assert path.read_text() == stock, "a rejected patch must not leave a broken file behind"


def test_wellformed_replacement_still_applies(tmp_path):
    """The parse check must not reject a correct patch."""
    path = tmp_path / "victim.py"
    path.write_text("def f():\n    return 1\n")
    plc._apply(
        str(path),
        present="# egg marker",
        needle="    return 1\n",
        replacement="    # egg marker\n    return 2\n",
        label="Patch X",
    )
    assert "return 2" in path.read_text()


def test_unparseable_input_is_not_blamed_on_the_patch(tmp_path):
    """The check asserts the patch did not break the file, not that the file
    was ever valid — the concatenated-needle fixtures here never are, and
    holding them to it would test the fixture rather than the patch."""
    path = tmp_path / "victim.py"
    path.write_text("this is (not python\n")
    plc._apply(
        str(path),
        present="# egg marker",
        needle="not python",
        replacement="# egg marker\nstill not python",
        label="Patch X",
    )
    assert "# egg marker" in path.read_text()


def test_installed_module_payload_must_parse(tmp_path, monkeypatch):
    """Same guard on the other write path: a truncated COPY or a half-written
    staged file would install without complaint and only fail at import."""
    spec = dict(plc.NEW_MODULES[0])
    (tmp_path / spec["dest"]).parent.mkdir(parents=True, exist_ok=True)

    broken = tmp_path / "staged"
    broken.mkdir()
    (broken / spec["source"]).write_text("def truncated(\n")
    monkeypatch.setattr(plc, "_module_source", lambda name, label: str(broken / name))

    with pytest.raises(SystemExit) as excinfo:
        plc._install_module(str(tmp_path), spec)
    assert "does not parse" in str(excinfo.value)
    assert not (tmp_path / spec["dest"]).exists()


def test_installed_module_parse_error_points_at_the_real_line(tmp_path, monkeypatch):
    """The reported line must be the one the operator will open.

    The provenance header is prepended before the file is written, so parsing
    the *payload* shifts every line by one and sends whoever reads the build log
    to the wrong place. This path also has no replacement in it — the staged
    source itself is broken — so ``_apply``'s vocabulary would misdescribe it."""
    spec = dict(plc.NEW_MODULES[0])
    (tmp_path / spec["dest"]).parent.mkdir(parents=True, exist_ok=True)

    broken = tmp_path / "staged"
    broken.mkdir()
    # Syntax error deliberately on line 5, well clear of the off-by-one.
    (broken / spec["source"]).write_text('"""Doc."""\n\nimport os\n\ndef truncated(\n')
    monkeypatch.setattr(plc, "_module_source", lambda name, label: str(broken / name))

    with pytest.raises(SystemExit) as excinfo:
        plc._install_module(str(tmp_path), spec)
    message = str(excinfo.value)
    assert f"{spec['source']}:5:" in message, f"expected the real line, got: {message}"
    assert "replacement" not in message, "no replacement is involved on the install path"
    assert "staged module source does not parse" in message


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


def test_capability_gate_is_additive_not_substitutive(tmp_path):
    """Patch 7 must UNION the live answer with the stock model-map answer.

    OpenRouter's ``supported_parameters`` under-reports ``reasoning_effort``
    (deepseek-r1 advertises only ``reasoning``), so letting live data win
    outright would drop a knob the map correctly allows — trading one silent
    drop for another. The stock ``supports_reasoning`` branch must therefore
    survive the patch."""
    patch7 = _patch_by_description("openrouter live capabilities")

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


def test_thinking_gate_leaves_the_claude_branch_alone(tmp_path):
    """Patch 9 must stop the adapter manufacturing a ``reasoning_effort``.

    On ``/v1/messages`` litellm derives ``reasoning_effort`` from the caller's
    ``thinking`` budget for every non-Claude model. That derived value is a cap
    BELOW the model default (#3624: kimi-k3 means 3130 reasoning tokens with no
    param vs 340 with ``high``), so Patch 7 alone would silently shallow every
    agent turn. The Claude branch, which forwards ``thinking`` unchanged, must
    be unaffected — and so must the assignments, which the gate returns before
    rather than rewriting."""
    patch9 = _patch_by_description("thinking->reasoning_effort synthesis gate")

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


def test_thinking_gate_does_not_suppress_an_explicit_effort(tmp_path):
    """The gate's scope is the *derived* bucket, not the whole function.

    Stock reaches the assignment two ways: from ``budget_tokens`` (a ceiling
    nobody asked for) or from ``output_config.effort`` on an adaptive request,
    which is the caller saying outright what they want. Suppressing the second
    would be discarding an instruction, not declining to invent one — a
    different change from the one the patch documents."""
    patch9 = _patch_by_description("thinking->reasoning_effort synthesis gate")

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


def test_drop_params_needle_disambiguates_the_two_drop_sites(tmp_path):
    """litellm 1.86.2 has TWO ``drop_params`` branches in utils.py.

    They share the identical ``if litellm.drop_params is True or (...)``
    condition; only what follows differs (a bare ``pass`` in the embeddings
    path, the pop loop in ``get_optional_params``). Matching on the shared
    condition would patch whichever came first — the same needle-uniqueness
    trap as Patch 4. A fixture carrying the sibling ``pass`` form first must be
    left untouched."""
    patch8 = _patch_by_description("drop_params visibility")

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


def test_cost_details_carry_runs_after_the_rebuild_not_before(tmp_path):
    """Patch 10 must land on the far side of ``Usage(**model_dump())``.

    That constructor deletes a ``cost`` attribute it is handed as None, so a
    carry inserted *before* the rebuild would be writing into an object the
    rebuild is entitled to discard — the patch would apply cleanly, the build
    would pass, and ``cost`` would still read null on every streamed call,
    which is the bug it exists to fix."""
    patch10 = _patch_by_description("streamed cost_details preservation")

    target = tmp_path / patch10["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch10["needle"])
    plc._apply(
        str(target),
        present=patch10["present"],
        needle=patch10["needle"],
        replacement=patch10["replacement"],
        label=patch10["label"],
    )
    result = target.read_text()

    rebuild = "        returned_usage = Usage(**returned_usage.model_dump())\n"
    assert rebuild in result, "the stock rebuild must survive the patch"
    assert result.index(rebuild) < result.index("_egg_carry_upstream_cost"), (
        "the carry must run after the rebuild, not before it"
    )
    assert result.index("_egg_carry_upstream_cost") < result.index("        return returned_usage")
    # An import failure must never propagate: this is on the response path.
    assert "except Exception:" in result


def test_pricing_hooks_the_unmapped_branch_and_leaves_the_stock_raise(tmp_path):
    """Patch 11 must be a fallback, not a replacement.

    It sits at the "isn't mapped yet" raise, so it runs only once every stock
    lookup has already failed: a slug the bundled map DOES carry keeps the
    bundled rate, and the live card can add a model but never reprice one.
    The stock ValueError must still be reachable, for the slug OpenRouter has
    not heard of either."""
    patch11 = _patch_by_description("openrouter live pricing")

    target = tmp_path / patch11["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch11["needle"])
    plc._apply(
        str(target),
        present=patch11["present"],
        needle=patch11["needle"],
        replacement=patch11["replacement"],
        label=patch11["label"],
    )
    result = target.read_text()

    assert result.count("raise ValueError(") == 1, "the stock raise must survive, exactly once"
    assert result.index("_egg_openrouter_cost_entry") < result.index("raise ValueError(")
    # The lookup is handed the provider so it can decline to answer for
    # anything that is not OpenRouter — this call site is generic.
    assert "_egg_openrouter_cost_entry(\n                        model, custom_llm_provider\n" in (
        result
    )
    # A missing or broken module leaves the stock behaviour exactly as it was.
    assert "except Exception:\n                    _egg_entry = None\n" in result


def test_pricing_needle_disambiguates_the_two_unmapped_messages(tmp_path):
    """utils.py carries the "isn't mapped yet" string twice.

    The other one is the outer handler's re-raise, with a different message
    body and indentation. Matching loosely would insert a pricing fallback into
    an exception handler, where ``_model_info`` and ``key`` are not even in
    scope — same needle-uniqueness trap as Patches 4 and 8."""
    patch11 = _patch_by_description("openrouter live pricing")

    sibling = (
        "SENTINEL_OUTER_HANDLER_BEGIN\n"
        "    except Exception as e:\n"
        '        verbose_logger.debug(f"Error getting model info: {e}")\n'
        "        raise Exception(\n"
        "            \"This model isn't mapped yet. model={}, custom_llm_provider={}. "
        "Add it here - https://github.com/BerriAI/litellm/blob/main/"
        'model_prices_and_context_window.json.".format(\n'
        "                model, custom_llm_provider\n"
        "            )\n"
        "        )\n"
        "SENTINEL_OUTER_HANDLER_END\n"
    )
    target = tmp_path / patch11["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch11["needle"] + "\n" + sibling)

    plc._apply(
        str(target),
        present=patch11["present"],
        needle=patch11["needle"],
        replacement=patch11["replacement"],
        label=patch11["label"],
    )
    result = target.read_text()

    assert result.count(patch11["present"]) == 1
    start = result.index("SENTINEL_OUTER_HANDLER_BEGIN")
    end = result.index("SENTINEL_OUTER_HANDLER_END") + len("SENTINEL_OUTER_HANDLER_END\n")
    assert result[start:end] == sibling, "patch 11 rewrote the outer handler's re-raise"
