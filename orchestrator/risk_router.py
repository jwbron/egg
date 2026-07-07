"""Deterministic risk router in front of the review graph (#3523 §4).

The review graph is static: every implement-phase slice draws all critical
lenses at full reasoning depth, whether it rewrites a lock-free queue handler
or fixes a typo in a README. That is uniform cost regardless of risk. This
module is the *pure* half of the fix — a deterministic function mapping a
slice's changed-file set to:

1. the set of review **lenses** that should run,
2. a **risk tier** that scales reasoning effort per role, and
3. an optional review **stance** (precision-first vs recall-first framing).

It is plain code, never a model: given the same changed files and the same
config it always returns the same decision. Nothing here reads the review
graph or the consensus wrapper, and (per the slice-5 contract) nothing in
``review_graph.py`` or ``consensus_wrapper.py`` imports this yet — the wiring
lands in a later slice (#3523 §S6). Keeping the core pure makes the floor
rules unit-testable in isolation.

Two responsibilities are split deliberately:

- :func:`route_slice` is **pure** (no filesystem, no clock) and takes an
  already-loaded :class:`RiskConfig`. This is the unit-testable core.
- :func:`load_risk_config` / :func:`default_config_path` do the I/O of
  reading ``.egg/review-risk.yaml`` off disk. A malformed config is a *loud*
  ``ValueError`` — a bad risk config must never silently narrow review.

HARD floor rules, encoded as pure logic (never as prompt guidance):

- **No-match => full graph + loud warning.** A slice whose changed files
  match no config entry returns the FULL lens set plus a warning signal.
  Missing config must never mean *less* review.
- **Floor tier.** Every slice is guaranteed at least :data:`FLOOR_TIER`; a
  misrouted-risky slice (unrouted files, or a security-sensitive path) is
  floored to :data:`MISROUTE_FLOOR_TIER` so it still gets a real review.
- **Security is un-gatable on protected paths.** A slice touching an
  auth / session / input-boundary path always runs :data:`REVIEWER_SECURITY`,
  even if a config entry omits it. This is structural — the protected-path
  set lives in code, not in the operator-editable YAML, so a config edit
  cannot drop the security lens off those paths.

Tiers map onto the ``/review`` effort ladder (low / medium / high / xhigh,
caps 4 / 8 / 10 / 15). See :data:`_TIER_EFFORT` / :data:`_TIER_REVIEW_CAP`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path

from egg_contracts.agent_roles import AgentRole
from egg_logging import get_logger
from egg_restrictions.matchers import match_pattern

logger = get_logger("orchestrator.risk_router")


# ---------------------------------------------------------------------------
# Lens universe
# ---------------------------------------------------------------------------

# Canonical implement-phase critical lenses. Mirrors
# ``egg_contracts.agent_roles._PHASE_REVIEWERS["implement"]`` — the deliberate
# duplication (rather than importing that semi-private table) keeps this pure
# module decoupled from the phase-roster wiring, which the slice-5 contract
# requires stay untouched. A drift guard in the tests pins these equal.
REVIEWER_CODE = AgentRole.REVIEWER_CODE.value
REVIEWER_CODE_HOLISTIC = AgentRole.REVIEWER_CODE_HOLISTIC.value
REVIEWER_CONTRACT = AgentRole.REVIEWER_CONTRACT.value
REVIEWER_SECURITY = AgentRole.REVIEWER_SECURITY.value
REVIEWER_CONCURRENCY = AgentRole.REVIEWER_CONCURRENCY.value

# The FULL lens set returned on a no-match (missing config never means less
# review). Ordered set modeled as a frozenset; callers order deterministically.
FULL_IMPLEMENT_LENSES: frozenset[str] = frozenset(
    {
        REVIEWER_CODE,
        REVIEWER_CODE_HOLISTIC,
        REVIEWER_CONTRACT,
        REVIEWER_SECURITY,
        REVIEWER_CONCURRENCY,
    }
)


# ---------------------------------------------------------------------------
# Risk tier <-> /review effort ladder
# ---------------------------------------------------------------------------


class RiskTier(IntEnum):
    """Risk tier driving per-role reasoning effort.

    ``IntEnum`` so ``max(...)`` aggregates tiers across a multi-file slice
    (the slice's tier is the highest tier any changed file demands).
    """

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    XHIGH = 4

    @property
    def effort(self) -> str:
        """Reasoning-effort label on the ``/review`` ladder."""
        return _TIER_EFFORT[self]

    @property
    def review_cap(self) -> int:
        """Per-review tool-call / finding cap on the ``/review`` ladder."""
        return _TIER_REVIEW_CAP[self]

    @property
    def config_name(self) -> str:
        """Lower-case name as written in ``review-risk.yaml``."""
        return self.name.lower()


# The ``/review`` ladder: low/medium/high/xhigh -> effort label and the
# per-review cap of 4/8/10/15 (see the /code-review skill's effort ladder).
# Tiers feed ``agent_model_resolution`` (effort) and the S4 per-finding cap.
_TIER_EFFORT: dict[RiskTier, str] = {
    RiskTier.LOW: "low",
    RiskTier.MEDIUM: "medium",
    RiskTier.HIGH: "high",
    RiskTier.XHIGH: "xhigh",
}

_TIER_REVIEW_CAP: dict[RiskTier, int] = {
    RiskTier.LOW: 4,
    RiskTier.MEDIUM: 8,
    RiskTier.HIGH: 10,
    RiskTier.XHIGH: 15,
}

_TIER_BY_NAME: dict[str, RiskTier] = {t.name.lower(): t for t in RiskTier}


# Absolute floor: every slice is guaranteed at least a LOW-tier real review.
# Docs-only slices route here (the cheapest tier / minimal graph) so cost
# tracks risk without ever dropping to zero review.
FLOOR_TIER: RiskTier = RiskTier.LOW

# Misroute / risky floor: a slice with unrouted files (matched no config) or
# touching a security-sensitive path is floored HERE, guaranteeing a
# misrouted-risky slice still gets a *deep* review rather than the cheapest.
MISROUTE_FLOOR_TIER: RiskTier = RiskTier.HIGH


# ---------------------------------------------------------------------------
# Review stance
# ---------------------------------------------------------------------------


class ReviewStance(Enum):
    """Optional stance framing selected by tier (#3523 §4 third bullet).

    Precision-first on trivial tiers (favor fewer, high-confidence findings);
    recall-first on high tiers (favor coverage). ``None`` = no stance override
    (leave the reviewer's default framing untouched).
    """

    PRECISION_FIRST = "precision_first"
    RECALL_FIRST = "recall_first"


def stance_for_tier(tier: RiskTier) -> ReviewStance | None:
    """Deterministic tier -> stance mapping (pure).

    LOW is trivial -> precision-first; HIGH/XHIGH are risky -> recall-first;
    MEDIUM keeps the default framing (``None``).
    """
    if tier == RiskTier.LOW:
        return ReviewStance.PRECISION_FIRST
    if tier in (RiskTier.HIGH, RiskTier.XHIGH):
        return ReviewStance.RECALL_FIRST
    return None


# ---------------------------------------------------------------------------
# Structurally-protected security paths (NOT operator-overridable)
# ---------------------------------------------------------------------------

# Auth / session / input-boundary paths on which the security lens can never
# be gated off — even if a config entry omits ``reviewer_security``. These
# live in code, not in review-risk.yaml, precisely so a config edit cannot
# drop the security lens off a protected path. Patterns use the canonical
# ``match_pattern`` grammar shared by every file-boundary layer (#2356).
SECURITY_SENSITIVE_GLOBS: tuple[str, ...] = (
    # Authentication / session / credential handling
    "**/*auth*",
    "**/*session*",
    "**/*credential*",
    "**/*secret*",
    "**/*token*",
    "**/*login*",
    "**/*password*",
    # Policy / access-boundary enforcement
    "**/*_policy.py",
    "**/*mode_gate*",
    "**/*phase_filter*",
    "**/*restriction*",
    "**/*sanitiz*",
    "**/*redact*",
    # The gateway is the whole trust boundary between agents and the world.
    "gateway/",
)


def is_security_sensitive(path: str) -> bool:
    """True if ``path`` is an auth/session/input-boundary path (pure).

    The security lens is un-gatable on these paths regardless of config.
    """
    return any(match_pattern(path, glob) for glob in SECURITY_SENSITIVE_GLOBS)


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskRule:
    """One ``glob -> {lenses, tier}`` mapping from ``review-risk.yaml``."""

    glob: str
    lenses: frozenset[str]
    tier: RiskTier


@dataclass(frozen=True)
class RiskConfig:
    """Loaded per-repo risk config.

    Rules are matched per changed file with *most-specific-glob* resolution
    (the glob with the most literal, non-wildcard characters wins), breaking
    ties by declaration order (first-match). This mirrors the deterministic
    resolution used by ``phase-permissions.json`` / ``repositories.yaml``.
    """

    schema_version: int
    rules: tuple[RiskRule, ...]


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskRouteDecision:
    """Result of routing a slice's changed-file set."""

    lenses: frozenset[str]
    tier: RiskTier
    stance: ReviewStance | None
    # True when one or more changed files matched no config rule. When set,
    # ``lenses`` is the FULL set and ``warnings`` carries the loud signal.
    unrouted: bool
    # True when a security-sensitive path forced the security lens on.
    forced_security: bool
    # Loud-warning strings (empty on a clean, fully-routed match).
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def effort(self) -> str:
        return self.tier.effort

    @property
    def review_cap(self) -> int:
        return self.tier.review_cap


# ---------------------------------------------------------------------------
# Glob specificity
# ---------------------------------------------------------------------------

_WILDCARD_CHARS = frozenset("*?[]")


def _glob_specificity(glob: str) -> int:
    """Count literal (non-wildcard) characters in a glob.

    Higher = more specific. Used to resolve overlapping rules by
    most-specific-glob; ties break by declaration order.
    """
    return sum(1 for ch in glob if ch not in _WILDCARD_CHARS)


def _most_specific_rule(path: str, rules: tuple[RiskRule, ...]) -> RiskRule | None:
    """Return the most-specific rule matching ``path``, or ``None``.

    Deterministic: among matching rules, the highest literal-char count
    wins; ties resolve to the earliest-declared rule (first-match).
    """
    best: RiskRule | None = None
    best_key: tuple[int, int] | None = None
    for idx, rule in enumerate(rules):
        if not match_pattern(path, rule.glob):
            continue
        # Higher specificity wins; on a tie, the lower index (earlier
        # declaration) wins, so we negate idx to keep the max-comparison.
        key = (_glob_specificity(rule.glob), -idx)
        if best_key is None or key > best_key:
            best = rule
            best_key = key
    return best


# ---------------------------------------------------------------------------
# Pure router core
# ---------------------------------------------------------------------------


def route_slice(
    changed_files: Iterable[str],
    config: RiskConfig,
) -> RiskRouteDecision:
    """Route a slice's changed files to lenses + tier + stance. **Pure.**

    Args:
        changed_files: Iterable of repo-relative changed-file paths.
        config: Loaded :class:`RiskConfig`.

    Returns:
        A :class:`RiskRouteDecision`. Floor rules are always applied:

        - Any changed file matching no rule => FULL lens set + a loud
          warning + tier floored to :data:`MISROUTE_FLOOR_TIER`.
        - A security-sensitive path => :data:`REVIEWER_SECURITY` forced on
          and tier floored to :data:`MISROUTE_FLOOR_TIER`.
        - Tier is never below :data:`FLOOR_TIER`.
    """
    # Normalize + de-dup + sort so the decision is order-independent and
    # deterministic across identical file sets in any order.
    files = sorted({_normalize(f) for f in changed_files if _normalize(f)})

    warnings: list[str] = []

    if not files:
        # Degenerate empty slice: never route to *less* than the full graph.
        warnings.append(
            "risk_router: empty changed-file set; defaulting to the FULL "
            "review graph at the misroute floor tier."
        )
        return RiskRouteDecision(
            lenses=FULL_IMPLEMENT_LENSES,
            tier=MISROUTE_FLOOR_TIER,
            stance=stance_for_tier(MISROUTE_FLOOR_TIER),
            unrouted=True,
            forced_security=False,
            warnings=tuple(warnings),
        )

    matched_lenses: set[str] = set()
    matched_tiers: list[RiskTier] = []
    unrouted_files: list[str] = []

    for path in files:
        rule = _most_specific_rule(path, config.rules)
        if rule is None:
            unrouted_files.append(path)
        else:
            matched_lenses |= rule.lenses
            matched_tiers.append(rule.tier)

    unrouted = bool(unrouted_files)
    security_paths = [p for p in files if is_security_sensitive(p)]
    forced_security = bool(security_paths)

    # --- Lens set -----------------------------------------------------------
    if unrouted:
        # HARD floor: missing config must never mean less review.
        warnings.append(
            "risk_router: "
            f"{len(unrouted_files)} changed file(s) matched no review-risk.yaml "
            "rule; running the FULL review graph. Unrouted: " + ", ".join(unrouted_files)
        )
        lenses = set(FULL_IMPLEMENT_LENSES)
    else:
        lenses = set(matched_lenses) or set(FULL_IMPLEMENT_LENSES)

    # HARD rule: security lens is un-gatable on protected paths.
    if forced_security and REVIEWER_SECURITY not in lenses:
        lenses.add(REVIEWER_SECURITY)
        warnings.append(
            "risk_router: security lens forced on for auth/session/"
            "input-boundary path(s): " + ", ".join(security_paths)
        )
    elif forced_security:
        # Already present, but record that these paths are structurally
        # protected so the wiring layer cannot later drop the lens.
        logger.debug("risk_router: security-sensitive paths present: %s", security_paths)

    # --- Tier ---------------------------------------------------------------
    base_tier = max(matched_tiers) if matched_tiers else FLOOR_TIER
    tier = max(base_tier, FLOOR_TIER)
    if unrouted or forced_security:
        # A misrouted-risky slice still gets a real (deep) review.
        tier = max(tier, MISROUTE_FLOOR_TIER)

    return RiskRouteDecision(
        lenses=frozenset(lenses),
        tier=tier,
        stance=stance_for_tier(tier),
        unrouted=unrouted,
        forced_security=forced_security,
        warnings=tuple(warnings),
    )


def _normalize(path: object) -> str:
    """Best-effort normalize a changed-file path for matching (pure)."""
    s = str(path).strip()
    if s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


# ---------------------------------------------------------------------------
# Config loading (I/O — kept out of the pure core)
# ---------------------------------------------------------------------------

# Operator override for the config location; otherwise resolved relative to
# the repo root, mirroring the ``.egg/phase-permissions.json`` convention.
RISK_CONFIG_ENV_VAR = "EGG_REVIEW_RISK_CONFIG"
_DEFAULT_CONFIG_RELPATH = os.path.join(".egg", "review-risk.yaml")

# Supported top-level schema version. Evolve additively.
SUPPORTED_SCHEMA_VERSION = 1


def default_config_path(repo_root: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the risk-config path (env override wins, else repo-relative)."""
    override = os.environ.get(RISK_CONFIG_ENV_VAR)
    if override:
        return Path(override)
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    return root / _DEFAULT_CONFIG_RELPATH


def load_risk_config(path: str | os.PathLike[str]) -> RiskConfig:
    """Load and validate ``review-risk.yaml`` into a :class:`RiskConfig`.

    A malformed config raises ``ValueError`` loudly — a bad risk config must
    never silently narrow review. Callers at the wiring seam decide the
    fail-open/closed posture (per the staged flag); the loader's job is to
    reject an unusable config unambiguously.
    """
    import yaml  # local import: yaml is only needed for the I/O path

    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"risk config not found: {p}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"risk config is not valid YAML ({p}): {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"risk config must be a mapping at top level ({p})")

    schema_version = raw.get("schema_version", 1)
    if not isinstance(schema_version, int):
        raise ValueError(f"risk config schema_version must be an int ({p})")
    if schema_version > SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"risk config schema_version {schema_version} is newer than "
            f"supported {SUPPORTED_SCHEMA_VERSION} ({p})"
        )

    raw_rules = raw.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError(f"risk config 'rules' must be a list ({p})")

    rules: list[RiskRule] = []
    for i, entry in enumerate(raw_rules):
        rules.append(_parse_rule(entry, index=i, source=str(p)))

    return RiskConfig(schema_version=schema_version, rules=tuple(rules))


def _parse_rule(entry: object, *, index: int, source: str) -> RiskRule:
    """Validate one rule mapping into a :class:`RiskRule`."""
    where = f"{source} rules[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: each rule must be a mapping")

    # ``match`` is the canonical key; accept ``glob`` as an alias.
    glob = entry.get("match", entry.get("glob"))
    if not isinstance(glob, str) or not glob.strip():
        raise ValueError(f"{where}: rule requires a non-empty 'match' glob")

    tier_name = entry.get("tier")
    if not isinstance(tier_name, str) or tier_name.lower() not in _TIER_BY_NAME:
        raise ValueError(
            f"{where}: 'tier' must be one of {sorted(_TIER_BY_NAME)}; got {tier_name!r}"
        )
    tier = _TIER_BY_NAME[tier_name.lower()]

    raw_lenses = entry.get("lenses", [])
    if not isinstance(raw_lenses, list) or not raw_lenses:
        raise ValueError(f"{where}: 'lenses' must be a non-empty list")
    lenses: set[str] = set()
    for lens in raw_lenses:
        if not isinstance(lens, str) or lens not in FULL_IMPLEMENT_LENSES:
            raise ValueError(
                f"{where}: unknown lens {lens!r}; valid lenses are {sorted(FULL_IMPLEMENT_LENSES)}"
            )
        lenses.add(lens)

    return RiskRule(glob=glob.strip(), lenses=frozenset(lenses), tier=tier)
