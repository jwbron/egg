# Analysis: Hello-world pilot task for #2799 Qwen routing comparison

> Issue: #2825 | Phase: refine

## Problem Statement

This issue is a **harness-validation task**, not a feature request. The deliverable is a tiny
pure-Python utility — `format_duration(seconds: float) -> str` — that renders durations as
human-readable strings (examples from the issue: `"1m 33s"`, `"2h 5m"`, `"450ms"`). The utility
must sensibly handle zero, negative, sub-second, and multi-hour inputs.

The *actual* purpose is to run a full SDLC pipeline end-to-end with `agent_models` overriding
every role in `egg_contracts.agent_roles.MODEL_OVERRIDE_ROLES` to `qwen3.7-max`, routed through
LiteLLM → OpenRouter → Qwen, and confirm that:

- The harness does **not** wedge (the context-window / auto-compaction edge case flagged in
  #2799 as the primary risk).
- Tool-call streaming (Read / Edit / Bash / MCP) does not silently truncate or malform under
  Qwen.
- Gateway egress to OpenRouter resolves (allowlist gap from #2799 is closed).
- LiteLLM / OpenRouter usage metrics surface input, output, **and reasoning** tokens — so the
  later real benchmark task can actually compute effective cost.
- Overseer / inspector roles continue to route to Claude (the #2812 / #2813 gap is in scope for
  the pilot to live with, not solve).

**Output quality is explicitly not a pass/fail criterion.** Qwen may produce a worse helper
than Opus would, and that is acceptable for this run.

The current pipeline run processing *this* issue is itself the validation vehicle — the refiner,
planner, coder, tester, documenter, and BRC reviewers in this run are all routed to Qwen 3.7
Max. The artifact produced (the `format_duration` helper) is a byproduct.

## Current Behavior

`egg` already contains a near-cousin helper that is **deliberately out of scope** for this issue
to consolidate or replace:

- `orchestrator/dag_visualizer.py:86` — `_format_seconds(total_seconds: int) -> str`. Private,
  integer-only, no sub-second support, format `"1m33s"` (no spaces). Used by the orchestrator
  DAG visualizer.
- Same module, `_format_duration(started_at, ended_at)` at `orchestrator/dag_visualizer.py:100`
  — wraps `_format_seconds` for `datetime` deltas. Different signature; not the helper this
  issue asks for.

The issue's explicit "Out of scope" list rules out wiring `format_duration` into existing
callers, so the existing private helper stays as-is.

No public `format_duration` exists today. `shared/pyproject.toml` lists `text_utils*` in
`tool.setuptools.packages.find.include`, but the corresponding `shared/text_utils/` directory
does not exist on disk. The planner can either resurrect that name, place the new helper in an
existing `shared/` subpackage (e.g. `egg_logging.formatters`), or pick another location — that
is a plan-phase call.

The upstream-routing wiring exercised by this pilot already exists:

- LiteLLM proxy + per-agent model routing landed in #2769 (see
  `docs/architecture/upstream-routing.md`).
- The `OPENROUTER_API_KEY` k3s secret was wired through to LiteLLM in #2815 (commit
  `6d5791861`).
- `MODEL_OVERRIDE_ROLES` is the authoritative set of roles whose `agent_models` override is
  honored (`shared/egg_contracts/agent_roles.py:1222`); validator at `orchestrator/models.py:796`
  rejects keys outside that set.

## Constraints

**From the issue body (binding):**

- Touches at most a handful of files (issue body).
- No infra / gateway / orchestrator-state changes — pure utility code.
- Real unit tests are required (so the tester role exercises pytest under Qwen).
- A short doc note is required (so the documenter role exercises a markdown edit under Qwen).
- No production caller is required; wiring `format_duration` into existing callers is out of
  scope.
- The behavior must handle zero, negative, sub-second, and multi-hour inputs sensibly. The
  exact spelling of "sensible" (e.g. negative → `"-450ms"` vs `"0s"` vs raise) is a plan-phase
  detail; the issue's examples (`"1m 33s"`, `"2h 5m"`, `"450ms"`) establish the visual style.

**Advisory seams (planner is free to choose differently):**

The change touches three areas that the planner will likely slice independently or co-locate as
it sees fit: (1) the new utility source file, (2) the unit-test file, (3) the docs note. Pinning
the slice shape is the planner's job; this list is informational only.

**Gateway file-write boundaries (relevant for the implement phase):**

- The coder role is blocked from writing to test paths (`tests/` etc.); the tester role writes
  tests. This pilot must produce both — plan should split tasks across roles accordingly.
- The documenter role is the only producer allowed to write under `docs/`. Plan must assign the
  doc-note task to the documenter, not the coder.

**Pilot-specific constraint (not an implementation constraint):**

- The pipeline submission for #2825 must set `agent_models` to map every role in
  `MODEL_OVERRIDE_ROLES` → `qwen3.7-max`. This is a runtime/operator action, not a code change
  in the PR.

## Options Considered

The deliverable's small size and the issue's explicit "Out of scope" list compress the option
space; the meaningful axes are *where* the helper lives and *how strict* the behavior is on
edge cases. These are documented as Options for the planner's reference; final selection is the
planner's call.

### Option A: New helper in an existing `shared/` subpackage

**Approach**: Add `format_duration` to an existing module such as
`shared/egg_logging/formatters.py` (which already contains formatting helpers, though log
formatters) or `shared/egg_contracts/` if framed as a contract-display utility. Tests go to
`shared/tests/test_format_duration.py` (or the chosen module's test file). Doc note appended
to an existing guide.

**Pros**:
- Zero new packages — minimum file footprint, smallest possible plumbing surface for Qwen to
  navigate.
- Stays inside packages the venv already installs editable.

**Cons**:
- Risks polluting a module with an off-topic helper (e.g. log formatters are
  GCP-Cloud-Logging-specific; duration formatting is unrelated).

### Option B: New `shared/text_utils/` module (the stub already in `pyproject.toml`)

**Approach**: Materialize `shared/text_utils/__init__.py` + `format.py` (or similar) and add
`format_duration` there. Tests in `shared/tests/test_text_utils.py`. Doc note in
`docs/reference/` or wherever the planner chooses.

**Pros**:
- Honors a name already declared in `pyproject.toml`'s `find.include` — minimal config churn.
- Gives future text utilities a natural home (cosmetic; not the goal of this issue).

**Cons**:
- More file footprint than Option A (`__init__.py`, a module file, a test file).

### Option C: Free-standing module under `shared/`

**Approach**: Add `shared/format_duration.py` directly at the top level of `shared/`, alongside
existing top-level packages. Probably **not** advisable — `shared/` is a packages-only
namespace and a bare `.py` would be unusual.

**Pros**: Smallest absolute footprint.

**Cons**: Inconsistent with repo convention.

## Recommended Approach

**Defer the module-location decision to the planner.** Both Option A and Option B are
defensible; the choice depends on the planner's read of which path produces the smallest,
most-mechanical change set under Qwen. The Recommended Approach for the analysis-level question
is:

1. The implementation is a single pure-Python function with no I/O, no dependencies beyond
   the stdlib, and no caller integration.
2. Tests cover: zero, negative, fractional seconds (`450ms`), boundary `1s` / `60s` / `3600s`,
   and a multi-hour value. Behavior on negative input is a planner choice — recommend the
   simplest sensible rule (e.g. `"0s"` for negative, or `"-<positive form>"`) and document it
   in the function's docstring.
3. Doc note is one paragraph in an existing markdown file (planner picks — likely a `docs/`
   reference or a small README). Do **not** introduce a brand-new top-level doc page for this.
4. The pipeline submission with the all-Qwen `agent_models` map is performed by the operator
   when launching the pipeline; it is not part of the PR's diff.

## Runtime-Primitive Assumptions Surfaced for the Plan Phase

Per #2594, naming the primitives the plan will depend on so the planner's Primitive-Existence
and Trust-Boundary audits are cheap:

- **Producer:** the function `format_duration(seconds: float) -> str` will be a top-level
  public symbol in a Python module under `shared/` (exact module is a plan decision; see
  Options above). Execution context: in-sandbox-agent during tests; pure compute, no I/O, no
  trust boundary crossed.
- **Tests:** pytest will discover the new test file via the existing `shared/tests/` test
  layout (`conftest.py` already present at `shared/tests/conftest.py`). Execution context:
  trusted-CI-runner (under `make test-all`) and in-sandbox-agent (under `make test`). No
  fixtures or env vars required.
- **Documenter artifact:** a markdown edit under `docs/` (existing file, planner picks).
  Execution context: human-operator (rendered by GitHub / repo viewers). No code-runtime
  primitive.
- **Gateway file-write boundaries** assumed to hold:
  - `coder` may write to `shared/**.py` (excluding `shared/tests/`).
  - `tester` may write to `shared/tests/**.py`.
  - `documenter` may write to `docs/**.md`.
  - These are the existing boundaries; no patterns need to change.
- **No new env vars, ConfigMap keys, CLI flags, decorators, fixtures, or routes** are
  introduced.
- **Upstream-routing primitives the pilot depends on (already in place; named for the audit,
  not because they need to be built):**
  - `agent_models: dict[str, str]` field on `PipelineConfig`
    (`orchestrator/models.py:757`).
  - `MODEL_OVERRIDE_ROLES` frozenset (`shared/egg_contracts/agent_roles.py:1222`).
  - LiteLLM proxy + OpenRouter alias `qwen3.7-max` (deployed via #2769 / #2815).
  - `OPENROUTER_API_KEY` k3s secret wired through to LiteLLM (#2815).

## Open Questions

None. The issue is highly specified: scope, deliverables, validation success criteria, and
"out of scope" list are all explicit; output quality is declared not pass/fail; the pipeline
submission parameters are stated verbatim. The remaining decisions (exact module location,
exact handling of negative input, exact doc-note location) are plan-phase design calls that do
not require human input — the operator's intent ("a small pure-Python utility, real tests, a
short doc note") fully constrains them.

If the planner discovers a question that genuinely requires operator input (e.g. gateway
boundary not covering the chosen module path), the planner can register it at the plan HITL
gate.

## Complexity Assessment

**low** — single-function, single-file (plus one test file and one doc edit). No
cross-component coupling, no schema changes, no infra changes, no new dependencies. The
validation surface (the Qwen routing) is large, but that's exercised by *running* the pipeline,
not by any code in the PR's diff.

---

*Authored-by: egg*
