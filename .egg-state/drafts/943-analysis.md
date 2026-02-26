# Analysis: Remove SDLC complexity tier 2 (mid), rename tier 3 (high) to tier 2

> Issue: #943 | Phase: refine

## Problem Statement

The SDLC pipeline currently has three complexity tiers (low, mid, high) but tiers 2 (mid) and 3 (high) don't have a meaningful behavioral distinction. Tier 2 is effectively a single-phase Tier 3 — both use multi-agent dispatch, the only difference is that Tier 3 supports per-phase implement cycles with optional parallelism. This artificial distinction adds cognitive overhead for the refine agent choosing a tier and for developers understanding the dispatch model.

**Current state**: Three tiers with overlapping semantics.
**Desired outcome**: Two tiers — low (short-circuit) and a single standard tier that subsumes both mid and high behavior.

## Current Behavior

### ComplexityTier Enum (`orchestrator/models.py:237-242`)

```python
class ComplexityTier(StrEnum):
    LOW = "low"    # Short-circuit: skip plan, single coder
    MID = "mid"    # Standard: full plan + single implement, wave-based multi-agent
    HIGH = "high"  # Complex: full plan + per-phase implement cycles, optional parallel
```

The `Pipeline.complexity_tier` field defaults to `ComplexityTier.MID` (line 315).

### Dispatch Routing (`orchestrator/routes/pipelines.py:5552-5601`)

The implement phase checks `pipeline.complexity_tier == ComplexityTier.HIGH` to decide between:
- **Tier 3 path** (`_run_tier3_implement()`): Per-plan-phase cycles (CODER→TESTER→DOCUMENTER→CHECKER→REVIEWER_CODE per phase), then a single INTEGRATOR with write access.
- **Tier 2 path** (`_run_multi_agent_phase()`): One wave-based execution across all roles.

### Tier-Aware Agent Permissions

- **`shared/egg_contracts/agent_roles.py:641`**: `get_role_definition()` checks `complexity_tier == "high"` to give the Integrator expanded write access (src/, lib/, docs/, tests/, shared/, etc.).
- **`gateway/agent_restrictions.py:526`**: `get_agent_pattern()` returns `INTEGRATOR_TIER3_PATTERNS` when `complexity_tier == "high"`.
- **`gateway/session_manager.py:296`**: Sessions store `complexity_tier` as a string for gateway validation.

### DAG Visualization (`orchestrator/dag_visualizer.py:893`)

When `pipeline.plan_phase_waves` is populated (Tier 3 only), the Implement phase renders expanded sub-phase boxes with fan-out/fan-in connectors instead of a single box.

## Constraints

- **Backward compatibility**: Existing pipelines stored in `.egg-state/` may reference `"mid"` or `"high"` string values. The `_check_high_complexity_signal()` function parses these from analysis YAML metadata blocks.
- **HealthTier is separate**: `HealthTier` in `orchestrator/health_checks/types.py` uses `PROGRAMMATIC`/`AGENT` values for a completely different concept — it must NOT be changed.
- **Session storage**: `gateway/session_manager.py` stores `complexity_tier` as a raw string, so serialized sessions in Redis may contain old values.
- **Analysis metadata format**: The YAML metadata block format (`complexity_tier: low/mid/high`) is hardcoded in the refine agent prompt (CLAUDE.md) and the analysis template (`docs/templates/analysis.md`).
- **Gateway string comparisons**: The gateway uses raw string comparisons (`complexity_tier == "high"`) rather than enum values, creating a coupling point.

## Scope Inventory

### Source Files (7 files)

| File | What Changes |
|------|-------------|
| `orchestrator/models.py` | `ComplexityTier` enum: remove `MID`, rename `HIGH`; update `Pipeline.complexity_tier` default |
| `orchestrator/routes/pipelines.py` | `_run_tier3_implement()` rename; dispatch condition update; `_check_high_complexity_signal()` update |
| `orchestrator/dag_visualizer.py` | `_render_tier3_implement()` rename; references in rendering logic |
| `shared/egg_contracts/agent_roles.py` | `get_role_definition()` tier check string update |
| `gateway/agent_restrictions.py` | `INTEGRATOR_TIER3_PATTERNS` rename; `get_agent_pattern()` string update |
| `gateway/session_manager.py` | Comment/docstring updates for `complexity_tier` field |
| `docs/templates/analysis.md` | Complexity tier options and metadata blocks |

### Test Files (8 files)

| File | What Changes |
|------|-------------|
| `orchestrator/tests/test_tier3_dispatch.py` | Enum value tests, dispatch tests; possible file rename |
| `orchestrator/tests/test_tier3_execute.py` | Tier 3 execution tests; possible file rename |
| `orchestrator/tests/test_dag_visualizer.py` | Tier 3 visualization tests |
| `orchestrator/tests/test_dag_visualizer_extended.py` | Extended DAG tests with tier 3 references |
| `orchestrator/tests/test_dag_visualizer_review_fixes.py` | DAG review fix tests with tier 3 references |
| `orchestrator/tests/test_short_circuit.py` | References `complexity_tier: high` in test fixtures |
| `gateway/tests/test_integrator_tier3.py` | Integrator tier 3 pattern tests; possible file rename |
| `gateway/tests/test_phase_filter_tier3.py` | Phase filter tier 3 tests; possible file rename |
| `shared/egg_contracts/tests/test_agent_roles_tier3.py` | Agent roles tier 3 tests; possible file rename |

### Documentation Files (4 files)

| File | What Changes |
|------|-------------|
| `docs/guides/sdlc-pipeline.md` | Tier table, execution model, phase-level dispatch section |
| `docs/architecture/orchestrator.md` | Tier 3 references, worktree docs, DAG visualization docs |
| `docs/templates/analysis.md` | Complexity tier options in metadata block |
| `gateway/README.md` | Tier-aware file access docs |

## Options Considered

### Option A: Rename HIGH→MID (reuse the "mid" string value)

**Approach**: Remove the `MID` enum member. Rename `HIGH` to `MID` so its string value becomes `"mid"`. The two remaining tiers are `LOW="low"` and `MID="mid"`. Internal identifiers change from `tier3` to `tier2`.

**Pros**:
- The string value `"mid"` already exists in serialized pipeline state and analysis metadata, reducing migration friction
- Maintains the conceptual mapping: low = simple, mid = standard+
- The default `Pipeline.complexity_tier` stays `ComplexityTier.MID` — no default change needed

**Cons**:
- The `"mid"` string value now maps to what was previously `"high"` behavior (phase-level dispatch), which could confuse anyone who remembers the old semantics
- Old analysis documents with `complexity_tier: mid` would be misinterpreted if re-parsed — they'd now trigger phase-level dispatch instead of standard dispatch

### Option B: Remove MID, keep HIGH (keep the "high" string value)

**Approach**: Remove the `MID` enum member. Keep `HIGH="high"` as-is. The two remaining tiers are `LOW="low"` and `HIGH="high"`. Rename internal identifiers from `tier3` to `tier2`.

**Pros**:
- Semantically accurate — the remaining non-low tier IS the former high tier
- No behavior mismatch for old analysis docs (old `"high"` still means phase-level dispatch)

**Cons**:
- Every current `"mid"` reference in the codebase changes to `"high"`
- The default `Pipeline.complexity_tier` must change from `ComplexityTier.MID` to `ComplexityTier.HIGH`
- The name "high" implies something exceptional/rare, but this becomes the standard tier
- Analysis metadata blocks that previously said `complexity_tier: mid` would need to say `complexity_tier: high`, which feels odd for standard features

### Option C: Collapse to LOW + new name (e.g., "standard")

**Approach**: Remove both `MID` and `HIGH`. Create a single non-low tier with a new name like `STANDARD="standard"`. Rename all tier3 internals to descriptive names.

**Pros**:
- Clean break — no ambiguity about old vs new semantics
- Descriptive name avoids the "is mid now high?" confusion

**Cons**:
- Largest change surface — every reference to both `"mid"` and `"high"` must change
- Introduces a new string value that doesn't exist anywhere in serialized state
- Breaks backward compatibility with all existing analysis metadata

## Recommended Approach

**Option A: Rename HIGH→MID** is recommended, matching the issue's proposed direction. The key reasons:

1. **Minimal disruption**: The `"mid"` string value already exists throughout the codebase as the default, so the Pipeline default stays the same.
2. **The issue explicitly proposes this**: "rename tier 3 (high) to tier 2" — the new tier 2 inherits tier 3's behavior.
3. **Clean conceptual model**: Low = skip plan, Mid = everything else (plan + per-phase dispatch + optional parallelism). The distinction was always artificial.
4. **The `_check_high_complexity_signal()` function** already defaults to `("mid", False)` when no signal is found — after the change, "mid" correctly routes to the (now only) standard dispatch path.

The concern about old `complexity_tier: mid` analysis docs being reinterpreted is mitigated by the fact that these are ephemeral pipeline artifacts, not long-lived state.

## Open Questions

All questions registered via `egg-orch decision create`:

### Decision 2: ComplexityTier Enum Values
**Question**: What should the new ComplexityTier enum values be? Current: LOW='low', MID='mid', HIGH='high'. Since we collapse to two tiers, the remaining non-low tier needs a name.
- Option A: `LOW='low' + MID='mid'` — rename HIGH to MID, the string value 'mid' stays
- Option B: `LOW='low' + HIGH='high'` — just remove MID, keep 'high' string value

### Decision 3: Internal Identifier Naming
**Question**: Should internal identifiers like function names, variable names, and constants be renamed from 'tier3' to 'tier2'? E.g. `_run_tier3_implement`→`_run_tier2_implement`, `INTEGRATOR_TIER3_PATTERNS`→`INTEGRATOR_TIER2_PATTERNS`.
- Option A: Yes — rename all internal 'tier3' references to 'tier2' for consistency
- Option B: No — remove tier numbering entirely, use descriptive names like `_run_phased_implement`, `INTEGRATOR_PHASED_PATTERNS`

### Decision 4: Test File Naming
**Question**: Should test files with 'tier3' in their name be renamed? E.g. `test_tier3_dispatch.py` → `test_tier2_dispatch.py` (or `test_phased_dispatch.py`).
- Option A: Yes — rename test files to match the new naming convention
- Option B: No — keep existing test file names, just update the content

### Decision 5: Pipeline Default Tier
**Question**: The Pipeline model default is currently `ComplexityTier.MID`. After removing MID, should the default change to the new tier 2 value (whatever it's called), or should it remain conceptually 'standard'?
- Option A: Default to the new tier 2 value (current behavior, just renamed)
- Option B: Default to LOW and require explicit tier selection for complex tasks

### Decision 6: CLAUDE.md Scope
**Question**: The analysis template (`docs/templates/analysis.md`) and the refine agent prompt in CLAUDE.md both contain hardcoded complexity tier metadata blocks. Should this task also update CLAUDE.md's refine instructions, or just the template?
- Option A: Update both CLAUDE.md and `docs/templates/analysis.md`
- Option B: Only update `docs/templates/analysis.md` (CLAUDE.md is updated separately)

---

*Authored-by: egg*

```yaml
# metadata
complexity_tier: high
parallel_phases: true
```
