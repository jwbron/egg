"""Exhaustive unit tests for the deterministic risk router (#3523 §4, slice-5).

The router (``orchestrator/risk_router.py``) is the *pure* half of the
risk-routing fix: given a slice's changed-file set and a loaded
:class:`RiskConfig`, it deterministically returns the review lenses to run,
a risk tier that scales reasoning effort, and an optional stance. Nothing here
touches the review graph or the consensus wrapper — that wiring lands in a
later slice — so every rule and every HARD floor is unit-testable in isolation.

Coverage map (task-5-2 acceptance):

* **Lens gating per path class** — concurrency lens only on the async/queue
  handler paths that ask for it; security lens always on auth/session/
  input-boundary paths; docs-only => the minimal graph.
* **No-match => full graph + loud warning** — a changed file matching no rule
  never means *less* review.
* **Floor tier always present** — tier is never below :data:`FLOOR_TIER`; a
  misrouted-risky slice floors to :data:`MISROUTE_FLOOR_TIER`.
* **Low-risk => cheapest tier** (cost default) — docs-only routes to the LOW
  tier / effort ``low`` / cap 4.
* **Determinism** — the same file set (in any order, called any number of
  times) yields an identical, equal decision.
* **Security-un-gatable invariant** — an explicit test that a config entry
  omitting ``reviewer_security`` on a protected path CANNOT drop the lens.

Tests build synthetic :class:`RiskConfig` objects for precise invariants and
also exercise the real, shipped ``.egg/review-risk.yaml`` so the router and the
per-repo policy are validated together.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from egg_contracts.agent_roles import _PHASE_REVIEWERS
from risk_router import (
    FLOOR_TIER,
    FULL_IMPLEMENT_LENSES,
    MISROUTE_FLOOR_TIER,
    REVIEWER_CODE,
    REVIEWER_CODE_HOLISTIC,
    REVIEWER_CONCURRENCY,
    REVIEWER_CONTRACT,
    REVIEWER_SECURITY,
    ReviewStance,
    RiskConfig,
    RiskRule,
    RiskTier,
    default_config_path,
    is_security_sensitive,
    load_risk_config,
    route_slice,
    stance_for_tier,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Repo root: this file is orchestrator/tests/test_risk_router.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_CONFIG = _REPO_ROOT / ".egg" / "review-risk.yaml"


def _rule(glob: str, lenses, tier: RiskTier) -> RiskRule:
    return RiskRule(glob=glob, lenses=frozenset(lenses), tier=tier)


def _config(*rules: RiskRule, schema_version: int = 1) -> RiskConfig:
    return RiskConfig(schema_version=schema_version, rules=tuple(rules))


# A small, explicit config covering every path class the router distinguishes.
def _classes_config() -> RiskConfig:
    return _config(
        # docs-only -> minimal graph, cheapest tier
        _rule("**/*.md", {REVIEWER_CODE}, RiskTier.LOW),
        # config/manifests -> contract lens, medium
        _rule("**/*.yaml", {REVIEWER_CODE, REVIEWER_CONTRACT}, RiskTier.MEDIUM),
        # concurrency-sensitive path -> concurrency lens, high
        _rule(
            "orchestrator/concurrent_executor.py",
            {REVIEWER_CODE, REVIEWER_CODE_HOLISTIC, REVIEWER_CONCURRENCY},
            RiskTier.HIGH,
        ),
        # general source -> full critical graph, high
        _rule(
            "orchestrator/",
            FULL_IMPLEMENT_LENSES,
            RiskTier.HIGH,
        ),
    )


# --------------------------------------------------------------------------- #
# Drift guard: the local FULL lens mirror must equal the phase-reviewer roster
# --------------------------------------------------------------------------- #


def test_full_lenses_mirror_phase_reviewers():
    """``FULL_IMPLEMENT_LENSES`` must stay equal to the implement roster.

    The router deliberately re-declares the critical lenses rather than
    importing the semi-private ``_PHASE_REVIEWERS`` table. This drift guard
    pins the duplication equal so the two cannot silently diverge.
    """
    roster = {r.value for r in _PHASE_REVIEWERS["implement"]}
    assert set(FULL_IMPLEMENT_LENSES) == roster
    # And each named constant is a member of that roster.
    for lens in (
        REVIEWER_CODE,
        REVIEWER_CODE_HOLISTIC,
        REVIEWER_CONTRACT,
        REVIEWER_SECURITY,
        REVIEWER_CONCURRENCY,
    ):
        assert lens in roster


# --------------------------------------------------------------------------- #
# Lens gating per path class
# --------------------------------------------------------------------------- #


def test_docs_only_minimal_graph_cheapest_tier():
    """docs-only => minimal graph (single lens), LOW tier / effort low / cap 4."""
    decision = route_slice(["README.md", "docs/guides/testing.md"], _classes_config())

    assert decision.lenses == frozenset({REVIEWER_CODE})
    # No heavyweight lenses leaked in on a docs-only slice.
    assert REVIEWER_CONCURRENCY not in decision.lenses
    assert REVIEWER_SECURITY not in decision.lenses
    assert REVIEWER_CONTRACT not in decision.lenses
    # Cheapest tier is the cost default.
    assert decision.tier is RiskTier.LOW
    assert decision.effort == "low"
    assert decision.review_cap == 4
    assert decision.unrouted is False
    assert decision.forced_security is False
    assert decision.warnings == ()


def test_concurrency_lens_only_on_concurrency_paths():
    """The concurrency lens appears on its path class and NOT on docs."""
    cfg = _classes_config()

    concurrency = route_slice(["orchestrator/concurrent_executor.py"], cfg)
    assert REVIEWER_CONCURRENCY in concurrency.lenses
    assert concurrency.tier is RiskTier.HIGH

    docs = route_slice(["README.md"], cfg)
    assert REVIEWER_CONCURRENCY not in docs.lenses


def test_config_path_class_adds_contract_lens_medium():
    """A .yaml manifest routes to the contract lens at MEDIUM (stance None)."""
    decision = route_slice(["config/litellm/models.yaml"], _classes_config())

    assert decision.lenses == frozenset({REVIEWER_CODE, REVIEWER_CONTRACT})
    assert decision.tier is RiskTier.MEDIUM
    assert decision.effort == "medium"
    assert decision.review_cap == 8
    # MEDIUM keeps the reviewer's default framing.
    assert decision.stance is None


def test_lens_union_across_multi_file_slice():
    """Lenses union and the tier is the highest any file demands."""
    decision = route_slice(
        ["README.md", "config/x.yaml", "orchestrator/concurrent_executor.py"],
        _classes_config(),
    )
    # Union of {code} | {code, contract} | {code, holistic, concurrency}
    assert decision.lenses == frozenset(
        {
            REVIEWER_CODE,
            REVIEWER_CONTRACT,
            REVIEWER_CODE_HOLISTIC,
            REVIEWER_CONCURRENCY,
        }
    )
    # Highest tier wins (HIGH from the concurrency path).
    assert decision.tier is RiskTier.HIGH


# --------------------------------------------------------------------------- #
# Most-specific-glob resolution + deterministic tie-break
# --------------------------------------------------------------------------- #


def test_most_specific_glob_wins_over_broad_glob():
    """The most literal-character glob resolves a file, not declaration order."""
    cfg = _config(
        _rule("**/*.py", {REVIEWER_CODE}, RiskTier.LOW),
        _rule(
            "orchestrator/concurrent_executor.py",
            {REVIEWER_CODE, REVIEWER_CONCURRENCY},
            RiskTier.HIGH,
        ),
    )
    decision = route_slice(["orchestrator/concurrent_executor.py"], cfg)
    # Specific rule wins: concurrency lens + HIGH tier, despite the broad
    # ``**/*.py`` rule being declared first.
    assert REVIEWER_CONCURRENCY in decision.lenses
    assert decision.tier is RiskTier.HIGH


def test_equal_specificity_tie_breaks_by_declaration_order():
    """Two equal-specificity matches resolve to the earliest-declared rule."""
    cfg = _config(
        _rule("**/*.py", {REVIEWER_CODE}, RiskTier.LOW),  # idx 0 wins the tie
        _rule("**/*.py", {REVIEWER_CONTRACT}, RiskTier.MEDIUM),  # idx 1 loses
    )
    decision = route_slice(["orchestrator/foo.py"], cfg)
    assert decision.lenses == frozenset({REVIEWER_CODE})
    assert decision.tier is RiskTier.LOW


# --------------------------------------------------------------------------- #
# No-match => full graph + loud warning
# --------------------------------------------------------------------------- #


def test_no_match_returns_full_graph_and_warning():
    """A file matching no rule => FULL lens set + a loud warning + HIGH floor."""
    cfg = _config(_rule("**/*.md", {REVIEWER_CODE}, RiskTier.LOW))
    decision = route_slice(["orchestrator/brand_new_module.py"], cfg)

    assert decision.lenses == FULL_IMPLEMENT_LENSES
    assert decision.unrouted is True
    assert decision.tier is MISROUTE_FLOOR_TIER  # HIGH
    assert decision.warnings  # non-empty loud signal
    assert any("brand_new_module.py" in w for w in decision.warnings)


def test_partial_match_still_routes_full_graph():
    """Any unrouted file forces the FULL graph even if siblings matched."""
    cfg = _config(_rule("**/*.md", {REVIEWER_CODE}, RiskTier.LOW))
    decision = route_slice(["README.md", "orchestrator/unmatched.py"], cfg)

    assert decision.unrouted is True
    assert decision.lenses == FULL_IMPLEMENT_LENSES
    assert decision.tier is MISROUTE_FLOOR_TIER


def test_empty_changed_set_defaults_to_full_graph_high():
    """A degenerate empty slice never routes to *less* than the full graph."""
    decision = route_slice([], _classes_config())

    assert decision.lenses == FULL_IMPLEMENT_LENSES
    assert decision.tier is MISROUTE_FLOOR_TIER
    assert decision.unrouted is True
    assert decision.forced_security is False
    assert decision.warnings


def test_whitespace_only_paths_treated_as_empty():
    """Blank / whitespace paths are normalized out => empty-slice behaviour."""
    decision = route_slice(["", "   ", "\t"], _classes_config())
    assert decision.unrouted is True
    assert decision.lenses == FULL_IMPLEMENT_LENSES
    assert decision.tier is MISROUTE_FLOOR_TIER


# --------------------------------------------------------------------------- #
# Floor tier guarantees
# --------------------------------------------------------------------------- #


def test_tier_never_below_floor():
    """Even the cheapest matched rule cannot route below :data:`FLOOR_TIER`."""
    cfg = _config(_rule("**/*.md", {REVIEWER_CODE}, RiskTier.LOW))
    decision = route_slice(["README.md"], cfg)
    assert decision.tier >= FLOOR_TIER
    assert decision.tier is RiskTier.LOW  # FLOOR_TIER is LOW


def test_misroute_floor_applies_on_unrouted():
    """Unrouted slices floor to :data:`MISROUTE_FLOOR_TIER`, not FLOOR_TIER."""
    cfg = _config(_rule("**/*.md", {REVIEWER_CODE}, RiskTier.LOW))
    decision = route_slice(["src/unknown.py"], cfg)
    assert decision.tier is MISROUTE_FLOOR_TIER
    assert MISROUTE_FLOOR_TIER > FLOOR_TIER  # sanity: the misroute floor is deeper


def test_forced_security_floors_low_tier_rule_to_high():
    """A LOW-tier rule on a protected path is floored to the misroute tier."""
    # Rule matches the file at LOW, but the path is security-sensitive, so the
    # tier must floor to MISROUTE_FLOOR_TIER (a risky slice gets a deep review).
    cfg = _config(_rule("**/*.py", {REVIEWER_CODE}, RiskTier.LOW))
    decision = route_slice(["services/auth_handler.py"], cfg)
    assert decision.forced_security is True
    assert decision.tier is MISROUTE_FLOOR_TIER


# --------------------------------------------------------------------------- #
# Security-un-gatable invariant (explicit)
# --------------------------------------------------------------------------- #


def test_security_lens_forced_on_when_config_omits_it():
    """A config entry omitting reviewer_security CANNOT drop it off a protected path.

    This is the core structural invariant: the protected-path set lives in code,
    so an operator-editable config cannot gate the security lens off an
    auth/session/input-boundary path.
    """
    # The rule deliberately lists ONLY reviewer_code — no security lens.
    cfg = _config(_rule("**/*.py", {REVIEWER_CODE}, RiskTier.LOW))
    decision = route_slice(["app/user_session.py"], cfg)

    assert REVIEWER_SECURITY in decision.lenses
    assert decision.forced_security is True
    # The router announces the structural override loudly.
    assert any("security" in w.lower() for w in decision.warnings)


@pytest.mark.parametrize(
    "path",
    [
        "app/auth_handler.py",
        "core/session_store.py",
        "svc/credential_vault.py",
        "x/secret_loader.py",
        "y/token_mint.py",
        "z/login_flow.py",
        "q/password_reset.py",
        "shared/egg_restrictions/patterns_policy.py",
        "gateway/phase_filter.py",
        "orchestrator/redaction.py",
        "shared/sanitize.py",
        "gateway/anything.py",
    ],
)
def test_protected_paths_force_security_lens(path):
    """Every protected path class forces the security lens even with a bare rule."""
    cfg = _config(_rule("**/*.py", {REVIEWER_CODE}, RiskTier.LOW))
    decision = route_slice([path], cfg)
    assert decision.forced_security is True
    assert REVIEWER_SECURITY in decision.lenses
    assert is_security_sensitive(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "orchestrator/concurrent_executor.py",
        "README.md",
        "docs/guides/testing.md",
        "config/models.yaml",
        "shared/utils/strings.py",
    ],
)
def test_non_protected_paths_do_not_force_security(path):
    """Ordinary paths are not security-sensitive and do not force the lens."""
    assert is_security_sensitive(path) is False


def test_security_already_present_no_duplicate_forced_warning():
    """When the rule already lists security, no 'forced on' warning is emitted.

    forced_security is still True (the path IS protected), but the lens was not
    *added*, so the loud 'forced on' warning must not fire.
    """
    cfg = _config(
        _rule("gateway/", {REVIEWER_CODE, REVIEWER_SECURITY}, RiskTier.XHIGH),
    )
    decision = route_slice(["gateway/router.py"], cfg)
    assert decision.forced_security is True
    assert REVIEWER_SECURITY in decision.lenses
    assert not any("forced on" in w for w in decision.warnings)


# --------------------------------------------------------------------------- #
# Stance mapping
# --------------------------------------------------------------------------- #


def test_stance_for_tier_mapping():
    """LOW => precision-first; HIGH/XHIGH => recall-first; MEDIUM => None."""
    assert stance_for_tier(RiskTier.LOW) is ReviewStance.PRECISION_FIRST
    assert stance_for_tier(RiskTier.MEDIUM) is None
    assert stance_for_tier(RiskTier.HIGH) is ReviewStance.RECALL_FIRST
    assert stance_for_tier(RiskTier.XHIGH) is ReviewStance.RECALL_FIRST


def test_decision_stance_tracks_tier():
    """The decision's stance is the stance for its resolved tier."""
    cfg = _classes_config()
    low = route_slice(["README.md"], cfg)
    assert low.stance is ReviewStance.PRECISION_FIRST

    high = route_slice(["orchestrator/concurrent_executor.py"], cfg)
    assert high.stance is ReviewStance.RECALL_FIRST


# --------------------------------------------------------------------------- #
# Tier <-> /review ladder mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tier", "effort", "cap"),
    [
        (RiskTier.LOW, "low", 4),
        (RiskTier.MEDIUM, "medium", 8),
        (RiskTier.HIGH, "high", 10),
        (RiskTier.XHIGH, "xhigh", 15),
    ],
)
def test_tier_ladder_effort_and_cap(tier, effort, cap):
    """Tiers map onto the /review ladder: low/medium/high/xhigh, caps 4/8/10/15."""
    assert tier.effort == effort
    assert tier.review_cap == cap
    assert tier.config_name == tier.name.lower()


def test_tier_is_intenum_ordered():
    """RiskTier is an IntEnum so max() aggregates tiers across a slice."""
    assert max(RiskTier.LOW, RiskTier.XHIGH, RiskTier.MEDIUM) is RiskTier.XHIGH
    assert RiskTier.LOW < RiskTier.HIGH


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_determinism_order_independent():
    """The same file set in different orders yields an equal decision."""
    cfg = _classes_config()
    files = ["orchestrator/concurrent_executor.py", "README.md", "config/x.yaml"]
    a = route_slice(files, cfg)
    b = route_slice(list(reversed(files)), cfg)
    assert a == b


def test_determinism_repeated_calls():
    """Calling the router twice on the same input returns an equal decision."""
    cfg = _classes_config()
    files = ["orchestrator/concurrent_executor.py", "README.md"]
    assert route_slice(files, cfg) == route_slice(files, cfg)


def test_determinism_duplicate_paths_collapse():
    """Duplicate / ``./``-prefixed paths normalize to the same decision."""
    cfg = _classes_config()
    a = route_slice(["README.md"], cfg)
    b = route_slice(["README.md", "./README.md", "README.md"], cfg)
    assert a == b


def test_decision_is_frozen():
    """RiskRouteDecision is immutable (frozen dataclass)."""
    decision = route_slice(["README.md"], _classes_config())
    with pytest.raises(FrozenInstanceError):
        decision.tier = RiskTier.XHIGH  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Real shipped config integration
# --------------------------------------------------------------------------- #


def test_real_config_loads():
    """The shipped ``.egg/review-risk.yaml`` loads into a valid RiskConfig."""
    cfg = load_risk_config(_REAL_CONFIG)
    assert isinstance(cfg, RiskConfig)
    assert cfg.schema_version == 1
    assert cfg.rules  # non-empty
    # Every rule carries a non-empty, valid lens set and a real tier.
    for rule in cfg.rules:
        assert rule.lenses
        assert rule.lenses <= FULL_IMPLEMENT_LENSES
        assert isinstance(rule.tier, RiskTier)


def test_real_config_docs_only_is_cheap():
    """Against the real policy, a docs-only slice is minimal graph + LOW."""
    cfg = load_risk_config(_REAL_CONFIG)
    decision = route_slice(["docs/index.md"], cfg)
    assert decision.tier is RiskTier.LOW
    assert decision.lenses == frozenset({REVIEWER_CODE})
    assert decision.forced_security is False


def test_real_config_gateway_is_xhigh_and_security_forced():
    """Against the real policy, gateway/ code is XHIGH and security-forced."""
    cfg = load_risk_config(_REAL_CONFIG)
    decision = route_slice(["gateway/phase_filter.py"], cfg)
    assert decision.tier is RiskTier.XHIGH
    assert REVIEWER_SECURITY in decision.lenses
    assert decision.forced_security is True


def test_real_config_concurrency_path_gets_concurrency_lens():
    """Against the real policy, the concurrent executor draws the concurrency lens."""
    cfg = load_risk_config(_REAL_CONFIG)
    decision = route_slice(["orchestrator/concurrent_executor.py"], cfg)
    assert REVIEWER_CONCURRENCY in decision.lenses
    assert decision.tier is RiskTier.HIGH


# --------------------------------------------------------------------------- #
# Config loading validation (malformed config fails loud)
# --------------------------------------------------------------------------- #


def test_load_missing_config_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_risk_config(tmp_path / "nope.yaml")


def test_load_non_mapping_top_level_raises(tmp_path):
    p = tmp_path / "risk.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping at top level"):
        load_risk_config(p)


def test_load_future_schema_version_raises(tmp_path):
    p = tmp_path / "risk.yaml"
    p.write_text("schema_version: 999\nrules: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="newer than"):
        load_risk_config(p)


def test_load_unknown_lens_raises(tmp_path):
    p = tmp_path / "risk.yaml"
    p.write_text(
        "schema_version: 1\n"
        "rules:\n"
        "  - match: '**/*.py'\n"
        "    lenses: [reviewer_bogus]\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown lens"):
        load_risk_config(p)


def test_load_bad_tier_raises(tmp_path):
    p = tmp_path / "risk.yaml"
    p.write_text(
        "schema_version: 1\n"
        "rules:\n"
        "  - match: '**/*.py'\n"
        "    lenses: [reviewer_code]\n"
        "    tier: ludicrous\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tier"):
        load_risk_config(p)


def test_load_empty_lenses_raises(tmp_path):
    p = tmp_path / "risk.yaml"
    p.write_text(
        "schema_version: 1\nrules:\n  - match: '**/*.py'\n    lenses: []\n    tier: low\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-empty list"):
        load_risk_config(p)


def test_load_missing_match_raises(tmp_path):
    p = tmp_path / "risk.yaml"
    p.write_text(
        "schema_version: 1\nrules:\n  - lenses: [reviewer_code]\n    tier: low\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-empty 'match' glob"):
        load_risk_config(p)


def test_default_config_path_env_override(monkeypatch, tmp_path):
    """The env var overrides the repo-relative default path."""
    override = tmp_path / "custom-risk.yaml"
    monkeypatch.setenv("EGG_REVIEW_RISK_CONFIG", str(override))
    assert default_config_path() == override


def test_default_config_path_repo_relative(monkeypatch):
    """Without the env override, the path is repo-relative to .egg/."""
    monkeypatch.delenv("EGG_REVIEW_RISK_CONFIG", raising=False)
    resolved = default_config_path(_REPO_ROOT)
    assert resolved == _REPO_ROOT / ".egg" / "review-risk.yaml"
