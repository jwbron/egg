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
