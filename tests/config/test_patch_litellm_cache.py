"""Unit tests for the egg-litellm build-time patch script.

``config/litellm/patch_litellm_cache.py`` edits the installed ``litellm``
package in place at image-build time (see PR #3190 / #3199). The real
``litellm==1.94.0`` is not a project dependency — and cannot run on the
repo's Python — so we exercise the patch *mechanics* against fixtures that
embed the exact needles the script targets, then assert the resulting
source. This is the patch-script regression the CI image build doesn't give
us: the build catches version drift via the fail-loud needle check, but
nothing else exercises idempotency, the replacement payload, or the
needle-uniqueness anchors on the two patches whose spelling appears twice in
``utils.py``.

Most fixtures are derived from ``PATCHES`` itself (the module-level spec
list), so there is no fixture/needle drift: a needle change automatically
flows into the fixture the test patches.

THE EXCEPTIONS ARE THE HAND-WRITTEN STOCK SNAPSHOTS — ``_STOCK_CAPABILITY_GATE``
and ``_STOCK_THINKING_TAIL_*`` below. They are transcribed from the pinned
litellm source so a test can assert what a patch leaves ALONE, which a
needle-derived fixture cannot express. Nothing checks them against the real
tree at test time (litellm is not installed here), so they are the one place
in this file that can silently become a snapshot of a version the image no
longer runs — exactly the false-pass shape ``_patch_by_description`` exists to
remove from the lookup key. ``_PINNED_LITELLM_VERSION`` is asserted against the
Dockerfile's ``FROM`` so a bump cannot land without this file being edited;
when you edit it, RE-TRANSCRIBE the snapshots from the new wheel rather than
bumping the constant alone.
"""

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "litellm"

# The litellm the hand-written stock snapshots in this file were transcribed
# from. Asserted against the Dockerfile's ``FROM`` below, so a version bump
# cannot land without someone editing this line — and the docstring above says
# what has to happen when they do.
_PINNED_LITELLM_VERSION = "1.94.0"


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


def test_stock_snapshots_are_pinned_to_the_image_litellm():
    """The hand-written snapshots below are only as good as their version.

    They cannot be checked against the real tree here — litellm is not a
    project dependency and does not run on the repo's Python — so the closest
    thing to a drift detector is making a version bump impossible to land
    without editing this file. At the 1.86.2 -> 1.94.0 bump BOTH snapshots
    changed (a reflowed gate, a new ``is_bedrock_arn_model`` arm) while every
    test kept passing, because a snapshot of an old version is still a valid
    fixture for the patch mechanics — it just stops describing the image."""
    dockerfile = (CONFIG_DIR / "Dockerfile").read_text()
    pins = [
        line.split(":v", 1)[1].strip()
        for line in dockerfile.splitlines()
        if line.startswith("FROM ghcr.io/berriai/litellm:v")
    ]
    assert pins == [_PINNED_LITELLM_VERSION], (
        f"Dockerfile pins litellm {pins} but the stock snapshots in this file were "
        f"transcribed from {_PINNED_LITELLM_VERSION}. Re-transcribe "
        "_STOCK_CAPABILITY_GATE and _STOCK_THINKING_TAIL_* from the new wheel, "
        "then update _PINNED_LITELLM_VERSION."
    )


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


def test_new_modules_are_installed_into_each_root(tmp_path):
    """``NEW_MODULES`` drops whole files that have no stock counterpart.

    The capability patch's gate imports
    ``litellm.llms.openrouter._egg_capabilities``, so if the module install
    silently no-ops the patched gate raises ImportError on every request —
    caught by its ``except Exception``, which would put us right back at the
    silent-drop behaviour the patch exists to remove."""
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


# The stock ``get_supported_openai_params`` gate, transcribed verbatim from
# litellm 1.94.0 (``llms/openrouter/chat/transformation.py``), picking up where
# the capability patch's needle ends. A PINNED SNAPSHOT — see the module
# docstring: 1.86.2 spelled this across three lines and 1.94.0 reflowed it, and
# nothing here would have noticed. Re-transcribe on every bump.
_STOCK_CAPABILITY_GATE = (
    "            if litellm.supports_reasoning("
    'model=model, custom_llm_provider="openrouter") or litellm.supports_reasoning(\n'
    "                model=model\n"
    "            ):\n"
    '                supported_params.append("reasoning_effort")\n'
    '                supported_params.append("thinking")\n'
    "        except Exception:\n"
    "            pass\n"
    "        return list(dict.fromkeys(supported_params))\n"
)


def test_capability_gate_is_additive_not_substitutive(tmp_path):
    """The capability patch must UNION live data with the stock model map.

    OpenRouter's ``supported_parameters`` under-reports ``reasoning_effort``
    (deepseek-r1 advertises only ``reasoning``), so letting live data win
    outright would drop a knob the map correctly allows — trading one silent
    drop for another. The stock ``supports_reasoning`` branch must therefore
    survive the patch."""
    capability_patch = _patch_by_description("openrouter live capabilities")

    # Applying the patch to the stock snapshot and asserting on the RESULT
    # tests the invariant the docstring claims; substring checks against the
    # replacement string alone would pass on a patch that deleted the branch.
    target = tmp_path / capability_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(capability_patch["needle"] + _STOCK_CAPABILITY_GATE)

    plc._apply(
        str(target),
        present=capability_patch["present"],
        needle=capability_patch["needle"],
        replacement=capability_patch["replacement"],
        label=capability_patch["label"],
    )
    result = target.read_text()

    # The live lookup is consulted...
    assert "_egg_capabilities" in result
    assert '"reasoning_effort" in _advertised' in result
    # ...and the stock model-map branch is still there, untouched, downstream
    # of it: the live answer can ADD a knob, never withhold one.
    assert _STOCK_CAPABILITY_GATE in result, (
        "the capability patch must not replace the stock model-map branch"
    )
    # The inserted block precedes it and does not return out of the function.
    inserted = result[: result.index(_STOCK_CAPABILITY_GATE)]
    assert "supported_params.append" in inserted
    assert "\n            return" not in inserted, (
        "the capability patch must not short-circuit the stock branch"
    )
    # Failures in the lookup must never propagate into a request.
    assert "except Exception:" in inserted
    # Only reasoning_effort is admitted. OpenRouter's `reasoning` field is a
    # different wire shape from Anthropic's `thinking`, not a spelling of it.
    assert '"thinking"' not in inserted


# The tail of ``_translate_thinking_to_openai`` as it stands in 1.94.0,
# verbatim, from the Claude branch through the assignments. The synthesis
# gate's needle is a slice of this; keeping the surrounding lines lets the test
# assert what the gate sits between, which is the whole invariant. A PINNED
# SNAPSHOT — see the module docstring. Both halves moved at the 1.86.2 -> 1.94.0
# bump (the Claude branch gained a ``is_bedrock_arn_model`` arm and the
# derivation call collapsed onto one line), which is exactly the drift nothing
# in this file can detect for you. Re-transcribe on every bump.
_STOCK_THINKING_TAIL_HEAD = (
    '        model = new_kwargs.get("model", "")\n'
    "        if self.is_anthropic_claude_model(model) or self.is_bedrock_arn_model(model):\n"
    '            new_kwargs["thinking"] = thinking  # type: ignore\n'
    "            return\n"
    "\n"
    "        reasoning_effort = self.translate_anthropic_thinking_to_reasoning_effort("
    "cast(Dict[str, Any], thinking))\n"
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
    """The synthesis gate must stop the adapter manufacturing an effort.

    On ``/v1/messages`` litellm derives ``reasoning_effort`` from the caller's
    ``thinking`` budget for every non-Claude model. That derived value is a cap
    BELOW the model default (#3624: kimi-k3 means 3130 reasoning tokens with no
    param vs 340 with ``high``), so the capability patch alone would silently
    shallow every agent turn. The Claude branch, which forwards ``thinking``
    unchanged, must be unaffected — and so must the assignments, which the gate
    returns before rather than rewriting."""
    gate_patch = _patch_by_description("thinking->reasoning_effort synthesis gate")

    target = tmp_path / gate_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _STOCK_THINKING_TAIL_HEAD + gate_patch["needle"] + _STOCK_THINKING_TAIL_FOOT,
    )

    plc._apply(
        str(target),
        present=gate_patch["present"],
        needle=gate_patch["needle"],
        replacement=gate_patch["replacement"],
        label=gate_patch["label"],
    )
    result = target.read_text()

    assert _STOCK_THINKING_TAIL_HEAD in result, "the gate must leave the Claude path alone"
    assert _STOCK_THINKING_TAIL_FOOT in result, "the gate must not rewrite the assignments"

    # The gate sits after the derivation and returns (rather than falling
    # through) when synthesis is off, so nothing is assigned.
    gate = result[result.index(gate_patch["present"]) : result.index(_STOCK_THINKING_TAIL_FOOT)]
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
    gate_patch = _patch_by_description("thinking->reasoning_effort synthesis gate")

    target = tmp_path / gate_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _STOCK_THINKING_TAIL_HEAD + gate_patch["needle"] + _STOCK_THINKING_TAIL_FOOT,
    )
    plc._apply(
        str(target),
        present=gate_patch["present"],
        needle=gate_patch["needle"],
        replacement=gate_patch["replacement"],
        label=gate_patch["label"],
    )
    result = target.read_text()

    # The adaptive override still runs, and it is what exempts the request.
    override = '                reasoning_effort = output_config["effort"]\n'
    assert override in result, "the adaptive override must survive the patch"
    assert result.index(override) < result.index(gate_patch["present"]), (
        "the gate must sit after the override, not before it"
    )
    assert f"{override}                _egg_effort_is_explicit = True\n" in result
    assert "if not _egg_effort_is_explicit:\n" in result


def test_drop_params_needle_disambiguates_the_two_drop_sites(tmp_path):
    """litellm 1.94.0 has TWO ``drop_params`` branches in utils.py.

    They share the identical ``if litellm.drop_params is True or (...)``
    condition; only what follows differs (a bare ``pass`` in the embeddings
    path, the pop loop in ``get_optional_params``). Matching on the shared
    condition would patch whichever came first — the same needle-uniqueness
    trap as the capability gate. A fixture carrying the sibling ``pass`` form
    first must be left untouched."""
    drop_patch = _patch_by_description("drop_params visibility")

    sibling = (
        "SENTINEL_PASS_SITE_BEGIN\n"
        "            if litellm.drop_params is True or (\n"
        "                drop_params is not None and drop_params is True\n"
        "            ):\n"
        "                pass\n"
        "SENTINEL_PASS_SITE_END\n"
    )
    fixture = sibling + "\n" + drop_patch["needle"]

    target = tmp_path / drop_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fixture)

    plc._apply(
        str(target),
        present=drop_patch["present"],
        needle=drop_patch["needle"],
        replacement=drop_patch["replacement"],
        label=drop_patch["label"],
    )
    result = target.read_text()

    assert result.count(drop_patch["present"]) == 1
    start = result.index("SENTINEL_PASS_SITE_BEGIN")
    end = result.index("SENTINEL_PASS_SITE_END") + len("SENTINEL_PASS_SITE_END\n")
    assert result[start:end] == sibling, (
        "the drop_params patch rewrote the embeddings-path drop site"
    )


def test_cost_details_carry_runs_after_the_rebuild_not_before(tmp_path):
    """The carry must land on the far side of ``Usage(**model_dump())``.

    That constructor drops the fields it is not declared to carry, so a carry
    inserted *before* the rebuild would be writing into an object the rebuild is
    entitled to discard — the patch would apply cleanly, the build would pass,
    and ``cost_details`` would still read null on every streamed call. On a BYOK
    route that is the whole bill: the provider-billed figure lives in
    ``cost_details.upstream_inference_cost`` and top-level ``cost`` is 0."""
    carry_patch = _patch_by_description("streamed cost_details preservation")

    target = tmp_path / carry_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(carry_patch["needle"])
    plc._apply(
        str(target),
        present=carry_patch["present"],
        needle=carry_patch["needle"],
        replacement=carry_patch["replacement"],
        label=carry_patch["label"],
    )
    result = target.read_text()

    rebuild = "        returned_usage = Usage(**returned_usage.model_dump())\n"
    assert rebuild in result, "the stock rebuild must survive the patch"
    assert result.index(rebuild) < result.index("_egg_carry_upstream_cost"), (
        "the carry must run after the rebuild, not before it"
    )
    assert result.index("_egg_carry_upstream_cost") < result.index("        return returned_usage")
    # An import failure must never propagate: this is on the response path.
    assert "except Exception as _egg_exc:" in result
    # And it must not be silent either — a swallowed import here looks exactly
    # like "the provider reported no cost", the symptom the patch removes.
    # verbose_logger is imported inside the handler because
    # streaming_chunk_builder_utils.py, unlike utils.py, does not carry it at
    # module scope; the latch is set only once the emit succeeded.
    assert "from litellm._logging import verbose_logger" in result
    assert result.index("verbose_logger.warning(") < result.index(
        "globals()['_egg_warned_stream_cost'] = True"
    ), "the latch must be set after the emit, not before it"


def test_roundtrip_needle_anchors_on_transform_request(tmp_path):
    """The round-trip patch must land in ``transform_request`` and nowhere else.

    ``_supports_cache_control_in_content`` is named THREE times in 1.94.0 (its
    own ``def``, the call in ``remove_cache_control_flag_from_messages_and_tools``
    and the call in ``transform_request``), and ``_move_cache_control_to_content``
    twice. A needle built from either call alone could retarget after an upstream
    reorder — the same trap the drop_params and live-pricing needles document.
    The needle therefore runs through the ``extra_body`` pop, which happens only
    here."""
    roundtrip_patch = _patch_by_description("assistant reasoning round-trip")

    # A sibling that mentions the same helper, ahead of the real site.
    sibling = (
        "    def remove_cache_control_flag_from_messages_and_tools(\n"
        "        self, model, messages, tools=None\n"
        "    ):\n"
        "        if self._supports_cache_control_in_content(model):\n"
        "            return messages, tools\n"
    )
    target = tmp_path / roundtrip_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(sibling + "\n" + roundtrip_patch["needle"])

    plc._apply(
        str(target),
        present=roundtrip_patch["present"],
        needle=roundtrip_patch["needle"],
        replacement=roundtrip_patch["replacement"],
        label=roundtrip_patch["label"],
    )
    result = target.read_text()

    assert sibling in result, "the sibling call site must be left alone"
    inserted_at = result.index(roundtrip_patch["present"])
    assert inserted_at > result.index(sibling), "the round-trip patch must land after the sibling"


def test_roundtrip_preserves_the_cache_control_step(tmp_path):
    """The round-trip patch shares its needle's file with Patch 1 and sits
    directly on top of the cache_control rewrite. Asserting on the RESULT
    (rather than on the replacement literal) is what catches a patch that
    dropped that step while inserting its own."""
    roundtrip_patch = _patch_by_description("assistant reasoning round-trip")

    target = tmp_path / roundtrip_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(roundtrip_patch["needle"])

    plc._apply(
        str(target),
        present=roundtrip_patch["present"],
        needle=roundtrip_patch["needle"],
        replacement=roundtrip_patch["replacement"],
        label=roundtrip_patch["label"],
    )
    result = target.read_text()

    # The cache_control step survives, and still precedes the extra_body pop.
    assert "messages = self._move_cache_control_to_content(messages)" in result
    assert result.index("_move_cache_control_to_content") < result.index("_egg_map_reasoning")
    assert result.index("_egg_map_reasoning") < result.index('optional_params.pop("extra_body"')

    # The mapping runs on the REQUEST path, and a failure in it cannot
    # propagate into the request.
    assert "except Exception:" in result
    assert "_egg_reasoning_roundtrip" in result


def test_roundtrip_passes_the_model_and_reports_an_unavailable_module(tmp_path):
    """Two properties of the injected call site.

    The module declines routes whose upstream re-verifies replayed reasoning
    (anthropic/*, google/*), which it can only do if the call site hands it
    ``model``. And an import failure means a broken image, not a bad request:
    reverting to stock in silence there is invisible until a model quietly
    reasons worse — the condition Patch 5 exists to stop repeating."""
    roundtrip_patch = _patch_by_description("assistant reasoning round-trip")

    target = tmp_path / roundtrip_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(roundtrip_patch["needle"])

    plc._apply(
        str(target),
        present=roundtrip_patch["present"],
        needle=roundtrip_patch["needle"],
        replacement=roundtrip_patch["replacement"],
        label=roundtrip_patch["label"],
    )
    result = target.read_text()

    assert "_egg_map_reasoning(messages, model)" in result
    assert "verbose_logger.warning(" in result
    assert "_egg_reasoning_roundtrip_warned" in result, "warned once, not once per request"


def test_roundtrip_maps_onto_the_request_not_the_response(tmp_path):
    """Guard against the adjacent-patch confusion the docstring calls out: the
    three patches the 1.94.0 bump retired were provider -> client, this one is
    client -> provider. A patch that touched ``transform_response`` would be a
    different bug."""
    roundtrip_patch = _patch_by_description("assistant reasoning round-trip")

    assert "transform_response" not in roundtrip_patch["needle"]
    assert "transform_response" not in roundtrip_patch["replacement"]
    assert roundtrip_patch["file"] == plc.F1


# The body ``_propagate_usage_cost_to_hidden_params`` runs once its guard is
# true, transcribed from 1.94.0 and picking up where the billable-cost patch's
# needle ends. A PINNED SNAPSHOT — see the module docstring. It is here rather
# than beside the other two because this test EXECUTES the patched result: an
# assertion that the gate rejects a BYOK zero is only meaningful if the thing
# behind the gate is the thing that actually sets the header.
_STOCK_HIDDEN_PARAMS_BODY = (
    '            if "additional_headers" not in response._hidden_params:\n'
    '                response._hidden_params["additional_headers"] = {}\n'
    '            response._hidden_params["additional_headers"]'
    '["llm_provider-x-litellm-response-cost"] = float(_usage.cost)\n'
)

_HIDDEN_PARAMS_SCAFFOLD = "class _Wrapper:\n    @staticmethod\n    def propagate(response):\n"


def _run_billable_cost_gate(tmp_path, cost):
    """Apply the billable-cost patch, then run the result against a fake usage.

    Returns the ``additional_headers`` dict the patched body produced, so the
    caller can assert on behaviour rather than on substrings."""
    gate_patch = _patch_by_description("billable-cost gate on hidden params")

    target = tmp_path / gate_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _HIDDEN_PARAMS_SCAFFOLD + gate_patch["needle"] + _STOCK_HIDDEN_PARAMS_BODY,
    )
    plc._apply(
        str(target),
        present=gate_patch["present"],
        needle=gate_patch["needle"],
        replacement=gate_patch["replacement"],
        label=gate_patch["label"],
    )

    namespace: dict = {}
    exec(compile(target.read_text(), str(target), "exec"), namespace)  # noqa: S102

    class _Usage:
        pass

    class _Response:
        pass

    usage = _Usage()
    if cost is not _MISSING:
        usage.cost = cost
    response = _Response()
    response.usage = usage
    response._hidden_params = {}

    namespace["_Wrapper"].propagate(response)
    return response._hidden_params.get("additional_headers", {})


_MISSING = object()
_COST_HEADER = "llm_provider-x-litellm-response-cost"


@pytest.mark.parametrize(
    "cost",
    [0.0, 0, -1.0, None, float("inf"), float("nan"), True, _MISSING],
)
def test_billable_cost_gate_declines_a_non_charge(tmp_path, cost):
    """The gate exists because 1.94.0 short-circuits its own cost calculator.

    ``response_cost_calculator`` returns this header's value before it ever
    reaches ``completion_cost()``, and stock propagates on ``is not None``.
    Under BYOK OpenRouter reports ``cost: 0.0`` — the real figure lives in
    ``cost_details`` — so stock hands the calculator a zero, ``response_cost``
    reads 0.0, egg's ``cost_estimated`` reads null, and the live rate-card patch
    (which lives inside ``completion_cost()``) becomes dead code on ~100% of
    egg's traffic. ``True`` is in the list because it is an ``int`` that is
    ``> 0``: a bool reaching a currency field is a bug upstream, not a bill."""
    assert _COST_HEADER not in _run_billable_cost_gate(tmp_path, cost)


def test_billable_cost_gate_propagates_a_real_charge(tmp_path):
    """The narrow fix, not blanket suppression.

    Where a provider does report a positive charge, upstream's propagation is
    an improvement over a token-based estimate and must survive untouched —
    ``cost_estimated`` then mirrors ``cost`` by design."""
    headers = _run_billable_cost_gate(tmp_path, 0.0031)
    assert headers[_COST_HEADER] == 0.0031


def test_pricing_hooks_the_unmapped_branch_and_leaves_the_stock_raise(tmp_path):
    """The live rate card must be a fallback, not a replacement.

    It sits at the "isn't mapped yet" raise, so it runs only once every stock
    lookup has already failed: a slug the bundled map DOES carry keeps the
    bundled rate, and the live card can add a model but never reprice one.
    The stock ValueError must still be reachable, for the slug OpenRouter has
    not heard of either."""
    pricing_patch = _patch_by_description("openrouter live pricing")

    target = tmp_path / pricing_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pricing_patch["needle"])
    plc._apply(
        str(target),
        present=pricing_patch["present"],
        needle=pricing_patch["needle"],
        replacement=pricing_patch["replacement"],
        label=pricing_patch["label"],
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
    assert "except Exception as _egg_exc:\n                    _egg_entry = None\n" in result
    # ...but not silently. A failed import is otherwise indistinguishable from
    # "OpenRouter has no rate for this slug", which is the null-cost_estimated
    # symptom the pricing patch exists to remove. Warned once, and the latch is set only
    # after the emit — inside its own try, because raising from an except block
    # would propagate into a live request.
    assert "_egg_warned_pricing" in result
    assert result.index("verbose_logger.warning(") < result.index(
        "globals()['_egg_warned_pricing'] = True"
    ), "the latch must be set after the emit, not before it"


# The indentation each replacement is spliced in at, and enough enclosing
# scope to make it a parseable module. Both bodies now carry a nested
# ``try``/``except`` for their warn-once latch, which is exactly the kind of
# hand-written indentation a string-literal patch payload gets wrong — and the
# build's own ``_check_parses`` would only catch it after a full image build.
_BODY_CONTEXTS = (
    (
        "streamed cost_details preservation",
        "class ChunkProcessor:\n"
        "    def calculate_usage(self, chunks):\n"
        "        Usage = dict\n"
        "        returned_usage = Usage()\n",
    ),
    (
        "openrouter live pricing",
        "def _get_model_info_helper(model, custom_llm_provider):\n"
        "    _model_info = None\n"
        "    key = None\n"
        "    if True:\n"
        "        if True:\n",
    ),
)


@pytest.mark.parametrize(
    ("description", "preamble"),
    _BODY_CONTEXTS,
    ids=["stream-cost", "openrouter-pricing"],
)
def test_patch_bodies_parse_at_their_insertion_indentation(description, preamble):
    """The spliced payload must be valid Python 3.11 — the image's interpreter.

    These replacements are string literals assembled line by line, so a wrong
    indent inside the nested warn-once handler is a plain typo that no other
    test here would catch: the ``_apply`` tests assert substrings, not syntax,
    and the build's ``_check_parses`` runs only during a real image build.
    ``feature_version`` pins the check to 3.11 for the same reason
    ``config/litellm/.ruff.toml`` does — this code runs on the litellm base
    image, not on the repo's interpreter.
    """
    patch = _patch_by_description(description)
    ast.parse(preamble + patch["replacement"], feature_version=(3, 11))


def test_pricing_needle_disambiguates_the_two_unmapped_messages(tmp_path):
    """utils.py carries the "isn't mapped yet" string twice.

    The other one is the outer handler's re-raise, with a different message
    body and indentation. Matching loosely would insert a pricing fallback into
    an exception handler, where ``_model_info`` and ``key`` are not even in
    scope — same needle-uniqueness trap as Patches 4 and 8."""
    pricing_patch = _patch_by_description("openrouter live pricing")

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
    target = tmp_path / pricing_patch["file"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pricing_patch["needle"] + "\n" + sibling)

    plc._apply(
        str(target),
        present=pricing_patch["present"],
        needle=pricing_patch["needle"],
        replacement=pricing_patch["replacement"],
        label=pricing_patch["label"],
    )
    result = target.read_text()

    assert result.count(pricing_patch["present"]) == 1
    start = result.index("SENTINEL_OUTER_HANDLER_BEGIN")
    end = result.index("SENTINEL_OUTER_HANDLER_END") + len("SENTINEL_OUTER_HANDLER_END\n")
    assert result[start:end] == sibling, "the pricing patch rewrote the outer handler's re-raise"
