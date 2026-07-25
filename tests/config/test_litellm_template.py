"""Regression guard: every litellm ``model_name`` must ship a paired ``[1m]`` alias.

The template at ``config/litellm-models.template.yaml`` is per-operator
config copied to ``~/.config/egg/litellm-models.yaml``. Each routed
``model_name: <x>`` row MUST ship with a sibling
``model_name: <x>[1m]`` row pointing at the same ``litellm_params``
(#2832). The bare and suffixed rows absorb Claude Code startup-probe
suffix leaks — without the alias registered, LiteLLM 400s those probes
with ``Invalid model name``.

This test file enforces three guarantees: every bare row has its
``[1m]`` alias, every ``[1m]`` alias has its bare sibling, and paired
rows share equal ``litellm_params`` (compared as parsed YAML) — so
probes and real requests can never be quietly routed through different
configs. It catches a
forgetting operator who adds a new backend without its paired alias or
lets the two rows' params drift. Commented-out example entries in the
template are YAML comments, not parsed entries, so they're naturally
excluded.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LITELLM_TEMPLATE = REPO_ROOT / "config" / "litellm-models.template.yaml"

_ALIAS_SUFFIX = "[1m]"


def _load_model_list() -> list[dict]:
    """Return the live ``model_list`` entries in the template."""
    outer = yaml.safe_load(LITELLM_TEMPLATE.read_text())
    inner_body = outer["data"]["config.yaml"]
    inner = yaml.safe_load(inner_body)
    return inner.get("model_list", []) or []


def _load_model_names() -> set[str]:
    """Return the set of live ``model_name`` values in the template."""
    return {entry["model_name"] for entry in _load_model_list()}


def _reasoning_effort_offenders(entries: list[dict]) -> list[str]:
    """Names of entries whose ``litellm_params.reasoning_effort`` is a silent
    no-op: set, but without ``model_info.supports_reasoning`` to make LiteLLM
    advertise the parameter as supported. See the class docstring below."""
    return sorted(
        entry["model_name"]
        for entry in entries
        if (entry.get("litellm_params") or {}).get("reasoning_effort") is not None
        and not (entry.get("model_info") or {}).get("supports_reasoning")
    )


class TestLitellmAliasInvariant:
    """Every bare ``model_name`` must have a paired ``<name>[1m]`` row,
    and vice versa, and the two rows must share the same
    ``litellm_params``. The symmetric naming errors — a bare row without
    its alias, or an ``[1m]`` alias without its bare sibling — and a
    ``litellm_params`` divergence between paired rows both mean probes
    and real requests are served by different routing tables.
    """

    def test_every_bare_model_has_1m_alias(self):
        names = _load_model_names()
        bare = sorted(n for n in names if not n.endswith(_ALIAS_SUFFIX))
        missing = [n for n in bare if f"{n}{_ALIAS_SUFFIX}" not in names]
        assert not missing, (
            "model_list entries without a paired `<name>[1m]` alias: "
            f"{missing} — add a sibling row pointing at the same "
            "`litellm_params` (see #2832, #2841)"
        )

    def test_no_orphaned_1m_alias(self):
        names = _load_model_names()
        suffixed = sorted(n for n in names if n.endswith(_ALIAS_SUFFIX))
        orphaned = [n for n in suffixed if n.removesuffix(_ALIAS_SUFFIX) not in names]
        assert not orphaned, (
            "model_list `[1m]` aliases without a bare sibling: "
            f"{orphaned} — add a `<name>` row pointing at the same "
            "`litellm_params` (see #2832, #2841)"
        )

    def test_paired_rows_share_model_info(self):
        # ``model_info`` is a sibling of ``litellm_params``, so the params
        # comparison above does not see it — but it carries
        # ``supports_reasoning``, which decides whether ``reasoning_effort``
        # reaches the wire at all. Divergence here means the probe alias and
        # the real row run at different reasoning depths.
        info = {entry["model_name"]: entry.get("model_info") for entry in _load_model_list()}
        diverged = sorted(
            name
            for name, bare_info in info.items()
            if not name.endswith(_ALIAS_SUFFIX)
            and f"{name}{_ALIAS_SUFFIX}" in info
            and info[f"{name}{_ALIAS_SUFFIX}"] != bare_info
        )
        assert not diverged, (
            "paired bare/`[1m]` rows with diverging `model_info`: "
            f"{diverged} — the `[1m]` alias must carry the same `model_info` "
            "as its bare sibling (notably `supports_reasoning`, which gates "
            "whether `reasoning_effort` is sent at all)"
        )

    def test_paired_rows_share_litellm_params(self):
        params = {entry["model_name"]: entry.get("litellm_params") for entry in _load_model_list()}
        diverged = sorted(
            name
            for name, bare_params in params.items()
            if not name.endswith(_ALIAS_SUFFIX)
            and f"{name}{_ALIAS_SUFFIX}" in params
            and params[f"{name}{_ALIAS_SUFFIX}"] != bare_params
        )
        assert not diverged, (
            "paired bare/`[1m]` rows with diverging `litellm_params`: "
            f"{diverged} — the `[1m]` alias must point at the same "
            "`litellm_params` as its bare sibling, or probes and real "
            "requests are quietly served by different configs (see #2832, #2841)"
        )


class TestReasoningEffortIsNotASilentNoop:
    """``litellm_params.reasoning_effort`` reaches the wire only when LiteLLM
    believes the model is reasoning-capable.

    ``OpenrouterConfig.get_supported_openai_params`` advertises
    ``reasoning_effort`` / ``thinking`` only if ``litellm.supports_reasoning``
    is true, and that reads LiteLLM's built-in model-cost map. A model absent
    from the map answers False, so the gate fails closed and ``drop_params:
    true`` then discards the parameter with no error and no log line.

    Absent is the normal state for an OpenRouter slug — verified against the
    pinned litellm 1.86.2, ``qwen/qwen3-max`` and
    ``moonshotai/kimi-k2-thinking`` are both absent and answer False, while
    ``deepseek/deepseek-r1`` is present and answers True. So the failure mode
    is not exotic: set ``reasoning_effort`` on a new slug and the agent
    silently runs at the provider's default depth.

    The fix is ``model_info: {supports_reasoning: true}`` on the same entry
    (verified to put ``reasoning_effort`` on the wire), or OpenRouter's native
    ``extra_body.reasoning.effort``, which bypasses the mapper entirely. This
    guard pins the first form so the template cannot ship the silent no-op."""

    def test_predicate_flags_the_silent_noop(self):
        # A guard that cannot fire is not a guard. Exercise it against the
        # exact shape #3599 reports in the wild: reasoning_effort set in
        # litellm_params, model not flagged.
        assert _reasoning_effort_offenders(
            [
                {
                    "model_name": "laguna-s-2.1",
                    "litellm_params": {"model": "openrouter/x/y", "reasoning_effort": "high"},
                }
            ]
        ) == ["laguna-s-2.1"]

    def test_predicate_accepts_the_flagged_form(self):
        assert (
            _reasoning_effort_offenders(
                [
                    {
                        "model_name": "ok",
                        "litellm_params": {"model": "openrouter/x/y", "reasoning_effort": "high"},
                        "model_info": {"supports_reasoning": True},
                    }
                ]
            )
            == []
        )

    def test_predicate_ignores_entries_not_setting_reasoning_effort(self):
        # The extra_body.reasoning form bypasses the mapper, so it needs no
        # model_info flag and must not be flagged as an offender.
        assert (
            _reasoning_effort_offenders(
                [
                    {
                        "model_name": "native",
                        "litellm_params": {
                            "model": "openrouter/x/y",
                            "extra_body": {"reasoning": {"effort": "high"}},
                        },
                    }
                ]
            )
            == []
        )

    def test_reasoning_effort_requires_supports_reasoning(self):
        offenders = _reasoning_effort_offenders(_load_model_list())
        assert not offenders, (
            "model_list entries setting `litellm_params.reasoning_effort` "
            f"without `model_info.supports_reasoning: true`: {offenders} — "
            "LiteLLM drops the parameter silently for any model it does not "
            "believe is reasoning-capable, so as written this is a no-op and "
            "the agent runs at the provider's default depth. Either add "
            "`model_info: {supports_reasoning: true}` to the entry, or use "
            "OpenRouter's native `extra_body.reasoning.effort` instead."
        )
