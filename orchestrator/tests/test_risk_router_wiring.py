"""Unit tests for the risk-router *wiring* layer (#3523 §4, slice-6 / task-6-3).

Slice-5 (``orchestrator/risk_router.py``) is the PURE half — a deterministic
function from a changed-file set to (lenses, tier, stance). Slice-6 is the
WIRING half: it threads that pure decision into the live review machinery behind
the ``EGG_RISK_ROUTER`` staged flag (``off`` default / ``log`` / ``on`` — the
shared staged shape ``slice_green_gate.green_gate_mode()`` uses, but keeping an
``off``-default, unlike that resolver which now defaults to ``log``). The wiring
lives in three seams:

* ``review_graph`` — ``risk_router_mode`` / ``resolve_risk_decision`` /
  ``apply_risk_router`` / ``risk_route_log_record`` and the ``changed_files``
  seam on :func:`get_review_graph_for_phase` (lens gating).
* ``agent_model_resolution.resolve_review_effort`` (+ the ``changed_files`` seam
  on :func:`resolve_agent_model`) — effort tiers.
* ``routes.pipelines._criteria._review_stance_framing`` — the single review
  stance conditional.

Coverage map (task-6-3 acceptance):

* **log-mode parity** — ``log`` records the would-be graph / tier / effort and
  changes NO real lens graph or effort (explicit parity vs. the full-graph
  baseline).
* **on-mode applies gating and effort.**
* **flag typo => off** — an unknown ``EGG_RISK_ROUTER`` value resolves to ``off``
  (never "silently review less").
* **no-match => full graph + logged warning.**
* **security lens un-gatable at the wiring layer** — the security lens survives
  on a protected path even when the routed lens set / config would gate it.
* **the stance conditional only fires in ``on`` mode.**

The flag resolver reads ``os.environ`` live, so every test pins
``EGG_RISK_ROUTER`` explicitly via ``monkeypatch`` and points
``EGG_REVIEW_RISK_CONFIG`` at a synthetic per-test config.
"""

from __future__ import annotations

import logging

import pytest
import review_graph
from agent_model_resolution import resolve_agent_model, resolve_review_effort
from review_graph import (
    apply_risk_router,
    get_default_implement_graph,
    get_review_graph_for_phase,
    resolve_risk_decision,
    risk_route_log_record,
    risk_router_mode,
)
from risk_router import (
    REVIEWER_CODE,
    REVIEWER_CODE_HOLISTIC,
    REVIEWER_CONCURRENCY,
    REVIEWER_CONTRACT,
    REVIEWER_SECURITY,
    ReviewStance,
    RiskRouteDecision,
    RiskTier,
)
from routes.pipelines._criteria import (
    _STANCE_PRECISION_FIRST,
    _STANCE_RECALL_FIRST,
    _get_reviewer_scope_preamble,
    _review_stance_framing,
    _reviewer_scope_preamble_body,
)

# --------------------------------------------------------------------------- #
# Synthetic config + fixtures
# --------------------------------------------------------------------------- #

# One config covering every path class the wiring must distinguish:
#   * docs (**/*.md)  -> LOW  / {reviewer_code}                 / precision stance
#   * config (**/*.yaml) -> MEDIUM / {reviewer_code, contract}  / no stance
#   * auth (**/*auth*)  -> matched at LOW but a PROTECTED path, so route_slice
#     forces the security lens on and floors the tier to the misroute (HIGH)
#     floor -> {reviewer_code, reviewer_security} / recall stance
# Anything else (a plain .py) matches no rule -> unrouted -> FULL graph + HIGH.
_CONFIG_YAML = """\
schema_version: 1
rules:
  - match: '**/*.md'
    lenses: [reviewer_code]
    tier: low
  - match: '**/*.yaml'
    lenses: [reviewer_code, reviewer_contract]
    tier: medium
  - match: '**/*auth*'
    lenses: [reviewer_code]
    tier: low
"""

# Representative changed-file sets for each routed class.
DOCS = ["README.md"]
CONFIG = ["config/models.yaml"]
AUTH = ["app/user_auth.py"]  # protected path -> security forced on
UNMATCHED = ["orchestrator/brand_new_module.py"]  # no rule -> full graph


@pytest.fixture
def risk_config(tmp_path, monkeypatch):
    """Write the synthetic risk config and point the loader at it (env override)."""
    cfg = tmp_path / "review-risk.yaml"
    cfg.write_text(_CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("EGG_REVIEW_RISK_CONFIG", str(cfg))
    return cfg


def _mode(monkeypatch, value: str) -> None:
    monkeypatch.setenv("EGG_RISK_ROUTER", value)


def _edge_set(graph):
    """A hashable, order-independent view of a graph's edges (frozen dataclasses)."""
    return set(graph.edges)


def _baseline_implement_edges():
    """The legacy implement graph edges (no router, no changed_files)."""
    return _edge_set(get_review_graph_for_phase("implement"))


# --------------------------------------------------------------------------- #
# Flag resolver: typo => off (never "silently review less")
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["on", "1", "true", "yes"])
def test_mode_on_synonyms(monkeypatch, value):
    _mode(monkeypatch, value)
    assert risk_router_mode() == "on"


@pytest.mark.parametrize("value", ["log", "log-only", "log_only"])
def test_mode_log_synonyms(monkeypatch, value):
    _mode(monkeypatch, value)
    assert risk_router_mode() == "log"


@pytest.mark.parametrize("value", ["onn", "enabled", "yep", "garbage", ""])
def test_mode_typo_resolves_to_off(monkeypatch, value):
    """An unknown / typo'd flag value must resolve to ``off`` (fail-safe)."""
    _mode(monkeypatch, value)
    assert risk_router_mode() == "off"


def test_mode_whitespace_and_case_normalized(monkeypatch):
    """Surrounding whitespace / case are normalized (``.strip().lower()``)."""
    _mode(monkeypatch, "  ON  ")
    assert risk_router_mode() == "on"
    _mode(monkeypatch, "LOG")
    assert risk_router_mode() == "log"


def test_mode_unset_defaults_off(monkeypatch):
    monkeypatch.delenv("EGG_RISK_ROUTER", raising=False)
    assert risk_router_mode() == "off"


# --------------------------------------------------------------------------- #
# Lens gating: off / log parity, on applies
# --------------------------------------------------------------------------- #


def test_off_mode_graph_is_byte_identical_baseline(monkeypatch, risk_config):
    """``off`` leaves the live graph byte-identical to legacy, even with files."""
    _mode(monkeypatch, "off")
    gated = get_review_graph_for_phase("implement", changed_files=DOCS)
    assert _edge_set(gated) == _baseline_implement_edges()


def test_log_mode_graph_parity_vs_full_baseline(monkeypatch, risk_config):
    """``log`` computes the would-be gating but runs the UNCHANGED full graph."""
    _mode(monkeypatch, "log")
    # DOCS would gate down to {reviewer_code, tester} under ``on`` — but in log
    # mode the live graph must still equal the full baseline.
    gated = get_review_graph_for_phase("implement", changed_files=DOCS)
    assert _edge_set(gated) == _baseline_implement_edges()


def test_on_mode_gates_docs_to_minimal_graph(monkeypatch, risk_config):
    """``on`` narrows the implement graph to the routed lens set (+ un-gate-ables)."""
    _mode(monkeypatch, "on")
    gated = get_review_graph_for_phase("implement", changed_files=DOCS)

    reviewers = gated.reviewer_roles()
    # Kept: the routed lens (reviewer_code) + the never-gate-able tester edge.
    assert reviewers == {REVIEWER_CODE, "tester"}
    # Dropped: every other critical implement lens.
    for dropped in (REVIEWER_CODE_HOLISTIC, REVIEWER_CONTRACT, REVIEWER_CONCURRENCY):
        assert dropped not in reviewers
    assert REVIEWER_SECURITY not in reviewers
    # It is a strict narrowing of the baseline, never an addition.
    assert _edge_set(gated) < _baseline_implement_edges()


def test_on_mode_tester_edge_never_gated(monkeypatch, risk_config):
    """The tester (dual-role, cold-start) is never dropped by gating."""
    _mode(monkeypatch, "on")
    gated = get_review_graph_for_phase("implement", changed_files=DOCS)
    # tester reviews coder — that critical edge must survive any gating.
    assert "tester" in gated.reviewer_roles()
    assert "coder" in gated.producers_for("tester")


def test_changed_files_none_is_baseline_regardless_of_mode(monkeypatch, risk_config):
    """No threaded changed_files => baseline graph even under ``on``."""
    _mode(monkeypatch, "on")
    graph = get_review_graph_for_phase("implement", changed_files=None)
    assert _edge_set(graph) == _baseline_implement_edges()


def test_gating_only_applies_to_implement_phase(monkeypatch, risk_config):
    """Refine/plan graphs are never narrowed, even under ``on`` with files."""
    _mode(monkeypatch, "on")
    for phase in ("refine", "plan"):
        baseline = _edge_set(get_review_graph_for_phase(phase))
        with_files = _edge_set(get_review_graph_for_phase(phase, changed_files=DOCS))
        assert with_files == baseline


# --------------------------------------------------------------------------- #
# No-match => full graph + logged warning
# --------------------------------------------------------------------------- #


def test_no_match_on_mode_keeps_full_graph_and_logs_warning(monkeypatch, risk_config):
    """An unrouted file => FULL graph (parity) + a loud logged warning."""
    _mode(monkeypatch, "on")

    captured: list[tuple[str, tuple]] = []

    class _Recorder:
        def warning(self, msg, *args, **kwargs):
            captured.append((msg, args))

        def info(self, msg, *args, **kwargs):
            pass

        def debug(self, msg, *args, **kwargs):
            pass

    monkeypatch.setattr(review_graph, "logger", _Recorder())

    gated = get_review_graph_for_phase("implement", changed_files=UNMATCHED)
    # Full graph is preserved: missing config / no-match never narrows review.
    assert _edge_set(gated) == _baseline_implement_edges()
    # A loud warning fired.
    assert captured, "expected a logged warning on an unrouted slice"
    assert any("brand_new_module.py" in str((m, a)) for m, a in captured)


def test_no_match_decision_is_full_and_warns(risk_config):
    """The decision itself carries the FULL lens set + a non-empty warning."""
    decision = resolve_risk_decision(UNMATCHED)
    assert decision is not None
    assert decision.unrouted is True
    assert decision.tier is RiskTier.HIGH  # MISROUTE floor
    assert decision.warnings


# --------------------------------------------------------------------------- #
# Fail-open: bad / missing config never narrows review
# --------------------------------------------------------------------------- #


def test_missing_config_falls_open_to_full_graph(monkeypatch, tmp_path):
    """A missing risk config (None decision) => full graph even under ``on``."""
    _mode(monkeypatch, "on")
    monkeypatch.setenv("EGG_REVIEW_RISK_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    gated = get_review_graph_for_phase("implement", changed_files=DOCS)
    assert _edge_set(gated) == _baseline_implement_edges()


def test_missing_config_resolve_decision_is_none(monkeypatch, tmp_path):
    """The shared fail-open resolver returns None on an unloadable config."""
    monkeypatch.setenv("EGG_REVIEW_RISK_CONFIG", str(tmp_path / "nope.yaml"))
    assert resolve_risk_decision(DOCS) is None


# --------------------------------------------------------------------------- #
# Security lens un-gatable AT THE WIRING LAYER (explicit)
# --------------------------------------------------------------------------- #


def test_apply_risk_router_reasserts_security_on_forced_path():
    """Even if the decision's lens set OMITS security, forced_security re-adds it.

    This is the wiring-layer re-assertion: ``apply_risk_router`` must not trust
    the pure core to have kept the security lens — a decision that forced
    security on (a protected path) keeps ``reviewer_security`` even when its
    ``lenses`` field does not list it.
    """
    decision = RiskRouteDecision(
        lenses=frozenset({REVIEWER_CODE}),  # deliberately omits reviewer_security
        tier=RiskTier.HIGH,
        stance=ReviewStance.RECALL_FIRST,
        unrouted=False,
        forced_security=True,
        warnings=(),
    )
    gated = apply_risk_router(get_default_implement_graph(), decision)
    assert REVIEWER_SECURITY in gated.reviewer_roles()


def test_apply_risk_router_gates_security_when_not_forced():
    """Control: with forced_security False, an omitted security lens IS dropped."""
    decision = RiskRouteDecision(
        lenses=frozenset({REVIEWER_CODE}),
        tier=RiskTier.LOW,
        stance=ReviewStance.PRECISION_FIRST,
        unrouted=False,
        forced_security=False,
        warnings=(),
    )
    gated = apply_risk_router(get_default_implement_graph(), decision)
    assert REVIEWER_SECURITY not in gated.reviewer_roles()


def test_security_survives_gating_on_protected_path_end_to_end(monkeypatch, risk_config):
    """Through the live seam: an auth path keeps the security lens even though the
    matching config rule lists only ``reviewer_code``."""
    _mode(monkeypatch, "on")
    gated = get_review_graph_for_phase("implement", changed_files=AUTH)
    reviewers = gated.reviewer_roles()
    assert REVIEWER_SECURITY in reviewers
    # The config rule only asked for reviewer_code, so the other pure-gate-able
    # lenses are dropped — security is the sole structural survivor.
    assert REVIEWER_CONCURRENCY not in reviewers
    assert REVIEWER_CODE_HOLISTIC not in reviewers


# --------------------------------------------------------------------------- #
# log-mode structural record
# --------------------------------------------------------------------------- #


def test_risk_route_log_record_shape(risk_config):
    """The log-mode record captures tier / effort / stance / dropped lenses."""
    decision = resolve_risk_decision(DOCS)
    assert decision is not None
    record = risk_route_log_record(get_default_implement_graph(), decision)

    assert record["mode"] == "log"
    assert record["risk_tier"] == "low"
    assert record["effort"] == "low"
    assert record["stance"] == "precision_first"
    assert record["lenses"] == [REVIEWER_CODE]
    # Docs gate down to {reviewer_code, tester}; the four critical lenses drop.
    assert record["dropped_lens_count"] == 4
    assert set(record["dropped_lenses"]) == {
        REVIEWER_CODE_HOLISTIC,
        REVIEWER_CONTRACT,
        REVIEWER_SECURITY,
        REVIEWER_CONCURRENCY,
    }
    assert record["unrouted"] is False
    assert record["forced_security"] is False


# --------------------------------------------------------------------------- #
# Effort tiers: off / log parity, on applies, fail-open
# --------------------------------------------------------------------------- #


def test_effort_off_returns_base(monkeypatch, risk_config):
    _mode(monkeypatch, "off")
    assert resolve_review_effort("medium", DOCS) == "medium"


def test_effort_changed_files_none_returns_base(monkeypatch, risk_config):
    _mode(monkeypatch, "on")
    assert resolve_review_effort("medium", None) == "medium"


def test_effort_on_applies_router_tier(monkeypatch, risk_config):
    """``on`` replaces the base effort with the router tier's ladder effort."""
    _mode(monkeypatch, "on")
    # DOCS -> LOW tier -> effort "low"; UNMATCHED -> HIGH tier -> effort "high".
    assert resolve_review_effort("medium", DOCS) == "low"
    assert resolve_review_effort("medium", UNMATCHED) == "high"


def test_effort_log_mode_parity(monkeypatch, risk_config, caplog):
    """``log`` records the would-be effort but returns the base unchanged."""
    _mode(monkeypatch, "log")
    with caplog.at_level(logging.INFO, logger="agent_model_resolution"):
        result = resolve_review_effort("medium", DOCS)
    # Parity: base effort is preserved despite the router routing DOCS -> low.
    assert result == "medium"
    # The would-be effort (low) is recorded for the soak.
    assert any("would pin review effort=low" in r.message for r in caplog.records)


def test_effort_typo_mode_returns_base(monkeypatch, risk_config):
    """A flag typo => off => base effort (never silently changed)."""
    _mode(monkeypatch, "onn")
    assert resolve_review_effort("medium", DOCS) == "medium"


def test_effort_missing_config_falls_open(monkeypatch, tmp_path):
    """A bad/missing config falls open to the base effort even under ``on``."""
    _mode(monkeypatch, "on")
    monkeypatch.setenv("EGG_REVIEW_RISK_CONFIG", str(tmp_path / "nope.yaml"))
    assert resolve_review_effort("medium", DOCS) == "medium"


def test_resolve_agent_model_effort_override_on(monkeypatch, risk_config):
    """The ``resolve_agent_model`` seam layers the router effort on ``on``."""
    _mode(monkeypatch, "on")
    base = resolve_agent_model("reviewer_code", None, None)
    # UNMATCHED routes to HIGH -> effort "high", overriding the base effort.
    routed = resolve_agent_model("reviewer_code", None, None, changed_files=UNMATCHED)
    assert routed.effort == "high"
    # Only the effort changed; the model decision is otherwise the base.
    assert routed.claude_code_alias == base.claude_code_alias
    assert routed.upstream == base.upstream
    assert routed.upstream_model == base.upstream_model


def test_resolve_agent_model_off_mode_is_base(monkeypatch, risk_config):
    """Under ``off``, threading changed_files leaves the decision at base effort."""
    _mode(monkeypatch, "off")
    base = resolve_agent_model("reviewer_code", None, None)
    routed = resolve_agent_model("reviewer_code", None, None, changed_files=UNMATCHED)
    assert routed.effort == base.effort


# --------------------------------------------------------------------------- #
# Stance conditional: only fires in ``on`` mode
# --------------------------------------------------------------------------- #


def test_stance_on_mode_low_tier_precision(monkeypatch, risk_config):
    _mode(monkeypatch, "on")
    assert _review_stance_framing(DOCS) == _STANCE_PRECISION_FIRST


def test_stance_on_mode_high_tier_recall(monkeypatch, risk_config):
    _mode(monkeypatch, "on")
    assert _review_stance_framing(UNMATCHED) == _STANCE_RECALL_FIRST


def test_stance_on_mode_medium_tier_neutral(monkeypatch, risk_config):
    """MEDIUM tier maps to no stance => empty framing even under ``on``."""
    _mode(monkeypatch, "on")
    assert _review_stance_framing(CONFIG) == ""


@pytest.mark.parametrize("mode", ["off", "log", "onn"])
def test_stance_only_fires_in_on_mode(monkeypatch, risk_config, mode):
    """``off`` / ``log`` / typo never append a stance framing."""
    _mode(monkeypatch, mode)
    assert _review_stance_framing(DOCS) == ""
    assert _review_stance_framing(UNMATCHED) == ""


def test_stance_none_changed_files(monkeypatch, risk_config):
    """No changed_files => no stance regardless of mode."""
    _mode(monkeypatch, "on")
    assert _review_stance_framing(None) == ""


def test_stance_fail_open_missing_config(monkeypatch, tmp_path):
    """A missing config (None decision) => no stance (fail-open)."""
    _mode(monkeypatch, "on")
    monkeypatch.setenv("EGG_REVIEW_RISK_CONFIG", str(tmp_path / "nope.yaml"))
    assert _review_stance_framing(DOCS) == ""


# --------------------------------------------------------------------------- #
# Scope preamble: stance is appended only under ``on``; body is legacy
# --------------------------------------------------------------------------- #


def test_scope_preamble_appends_stance_on_mode(monkeypatch, risk_config):
    """Under ``on``, the routed stance is appended to the legacy preamble body."""
    _mode(monkeypatch, "on")
    body = _reviewer_scope_preamble_body("code", "implement")
    preamble = _get_reviewer_scope_preamble("code", "implement", changed_files=DOCS)
    assert preamble == body + _STANCE_PRECISION_FIRST


@pytest.mark.parametrize("mode", ["off", "log"])
def test_scope_preamble_byte_identical_when_not_on(monkeypatch, risk_config, mode):
    """``off`` / ``log`` leave the reviewer preamble byte-identical to legacy."""
    _mode(monkeypatch, mode)
    body = _reviewer_scope_preamble_body("code", "implement")
    preamble = _get_reviewer_scope_preamble("code", "implement", changed_files=DOCS)
    assert preamble == body


def test_scope_preamble_no_changed_files_is_legacy(monkeypatch, risk_config):
    """No threaded changed_files => legacy preamble even under ``on``."""
    _mode(monkeypatch, "on")
    body = _reviewer_scope_preamble_body("code", "implement")
    assert _get_reviewer_scope_preamble("code", "implement") == body
