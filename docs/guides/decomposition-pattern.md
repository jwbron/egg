# File Decomposition Pattern

> Canonical pattern for decomposing oversized Python source files
> ratcheted by `scripts/file-size-allowlist.yaml`. This is the shape
> every slice in issue #2261 follows; future contributors who need to
> decompose another file should follow the same recipe so the codebase
> grows one consistent seam shape rather than a handful of bespoke
> ones.

## Why we decompose

The `make lint` check added in #2250 (closing #2248) caps each Python
source file at **1,500 lines / 100 KB**. The Read tool's soft 25k-token
budget is the floor under that cap: an agent that has to load a 16k-line
module to understand a single function pays the same context cost on
every BRC cycle. Decomposing a file once amortises that cost across all
future agents that touch the area.

Files that exceed the cap are grandfathered in
`scripts/file-size-allowlist.yaml`. The lint enforces a **ratchet**:
each PR that touches an allowlisted file must reduce its size, never
grow it. The decomposition program drives those baselines down to
zero.

The complete acceptance contract for this program lives in issue
[#2261](https://github.com/jwbron/egg/issues/2261); the HITL decisions
that locked the pattern shape (decisions 1, 5, 6, 7, 8) are
referenced inline below.

## (a) Sub-package layout

The chosen shape is a **sub-package with an explicit re-export
barrel** (HITL decision-1, decision-5, decision-6).

```
orchestrator/routes/pipelines.py     →     orchestrator/routes/pipelines/
                                              __init__.py        # barrel
                                              _run_loop.py       # cluster
                                              _criteria.py       # cluster
                                              _pr_lifecycle.py   # cluster
                                              _decisions.py      # cluster
                                              ...
```

Rules:

1. **Sub-package, not sibling files.** The original file becomes a
   directory `<name>/__init__.py`; clusters live as private
   `_<cluster>.py` siblings inside the directory. Sibling-file façades
   (`pipelines.py` plus `_pipelines_run.py`) were rejected in
   decision-1 because the resulting filenames are uglier and the
   import surface drifts.
2. **Underscore-prefixed submodules** (decision-6). `_run_loop.py`,
   `_criteria.py`, `_pr_lifecycle.py`. The leading underscore signals
   "package-private internal API" and matches existing repo
   convention. Promoting submodules to part of the public namespace
   is explicitly out of scope.
3. **Explicit per-symbol re-exports in `__init__.py`** (decision-5).
   `from ._run_loop import _run_pipeline, _spawn_pipeline_run_thread`
   — every symbol the barrel re-exports is listed by name. Wildcard
   `from ._run_loop import *` was rejected because it makes drift
   invisible to code review and trips some linters.
4. **Dual-import shape stays consistent inside a sub-package.**
   Existing modules use the
   `try: from ..foo import X` / `except ImportError: from foo import X`
   pattern to support both flat and packaged execution. New
   submodules **either** all use the dual-import shape **or** all use
   relative imports — never mix the two within one sub-package.

### What the barrel guarantees

The barrel is the **stable API surface** (decision-7). External
consumers — tests, production importers, mocks — keep importing
through the barrel:

```python
# Stable across decompositions
from routes.pipelines import _read_phase_draft

# Brittle: couples the consumer to internal layout
from routes.pipelines._readers import _read_phase_draft
```

Tests already lean on the stable form heavily — `unittest.mock.patch`
calls like `patch("routes.pipelines._foo")` resolve through whatever
the barrel re-exports, so a fresh decomposition does not break them.
**A submodule extraction that drops the barrel re-export is a
regression**, not a refactor.

## (b) File→package conversion mechanics ("step 0")

Python forbids `routes/pipelines.py` and `routes/pipelines/__init__.py`
from coexisting. The conversion is therefore a two-step move that has
to land as a clean baseline before any cluster work begins:

```bash
# Step 0 — bisectable baseline commit
git mv orchestrator/routes/pipelines.py orchestrator/routes/pipelines/__init__.py

# Verify imports still resolve before extracting anything
make test-all
git commit -m "refactor: convert routes/pipelines.py to sub-package baseline"
```

Only after the baseline commit is green do you start carving out
clusters into private submodules. Treating step 0 as its own commit
gives `git bisect` a clean cut between "moved the file" and "extracted
the cluster": a regression that surfaces during the cluster work
points at the cluster, not at the move.

> **Why the two-step matters.** A single commit that both moves and
> extracts will appear in `git blame` as if the original symbols were
> rewritten in the move. Keeping step 0 isolated preserves authorship
> history and makes the diff reviewer's job tractable.

## (c) Method-modules-on-class pattern

Some files are dominated by a single large class with many methods
(e.g. `orchestrator/mcp_tools.py`'s `PipelineToolHandler` with ~30
`_handle_*` methods, or `orchestrator/gateway_client.py`'s
`GatewayClient`). For these, the canonical decomposition is:

1. **Keep the class identity in `__init__.py`.** Tests patching
   `mcp_tools.PipelineToolHandler` keep working because the class
   object is still attached to the same module path.
2. **Extract method bodies into module-level functions** in
   underscore-prefixed submodules grouped by responsibility
   (`_dispatch.py`, `_status.py`, `_health.py`). Each helper takes
   the previously-implicit `self` argument explicitly.
3. **Methods become thin wrappers** that delegate to the module-level
   helpers:

   ```python
   # __init__.py
   from . import _dispatch, _status

   class PipelineToolHandler:
       def handle_dispatch(self, *args, **kwargs):
           return _dispatch.handle_dispatch(self, *args, **kwargs)

       def handle_status(self, *args, **kwargs):
           return _status.handle_status(self, *args, **kwargs)
   ```
4. **The barrel re-exports the class and any module-level helpers
   that have external consumers.** Mocks targeting either path keep
   resolving.

This shape preserves the class as the public surface while pushing
the bulk of the line count into testable, importable functions.
Pre-allocate the cluster files in the slice plan rather than carving
them out ad-hoc — uneven splits surface as "still over the cap" lint
failures during the slice's PR.

## (d) External-importer audit recipe

Every slice template includes an audit task that enumerates the
external references to every public symbol in the file being split.
The canonical incantation:

```bash
# Replace <module> with the slash-shaped module path (e.g. routes/pipelines)
# and <symbol> with the symbol name; the regex handles attribute access,
# `from … import …`, and `import … as …` shapes.
git grep -nE "(<module>\.|from .* import .*\b<symbol>\b)" -- '*.py'
```

Re-export every symbol that has any external reference (feedback Q6
of the refine phase). Skip re-exports **only** for symbols that are
both:

1. underscore-prefixed (`_my_helper`, not `my_helper`), AND
2. referenced exclusively inside the file's own package (no hits in
   `tests/`, `gateway/`, `orchestrator/`, `sandbox/`, or `shared/`
   outside the file's own directory).

Same-name symbols across different packages are common (e.g.
`_pipeline_identifier` exists in multiple modules); the
`<module>\.` half of the regex disambiguates them.

> **Why audit before extracting.** Missing a re-export shows up later
> as `AttributeError: module 'routes.pipelines' has no attribute
> '_foo'` in the test that patches `_foo`. Catching it at the audit
> step costs minutes; catching it after the slice's PR opens costs a
> review cycle.

## (e) Allowlist drop + rebase recipe

Each slice removes its file's entry from
`scripts/file-size-allowlist.yaml`. The lint already enforces this —
do not bypass it with a fresh allowlist entry.

With `EGG_ORCH_MAX_PARALLEL_SLICES=2`, this YAML file conflicts
mechanically across parallel slices. The rebase recipe:

```bash
# 1. Pull the latest parent branch
git fetch origin

# 2. Rebase onto the parent
git rebase origin/<parent-branch>

# 3. Conflict in scripts/file-size-allowlist.yaml is expected — take
#    "theirs" for any unrelated entries (other slices' removals) and
#    re-apply your own removal manually.
$EDITOR scripts/file-size-allowlist.yaml

# 4. Re-stage and continue
git add scripts/file-size-allowlist.yaml
git rebase --continue

# 5. Re-run lint to verify the file is still well-formed and the
#    allowlist still matches reality.
make lint
```

If a submodule extraction lands a file that is itself over the cap,
**split it further within the same slice** rather than adding a fresh
allowlist entry — see section (g).

## (f) Routes-handling convention

Both `orchestrator/routes/*.py` (Flask Blueprints) and
`gateway/gateway.py` (`@app.route(...)`) follow the **same** seam:

1. **Route registrations stay in `__init__.py`.** Decorators
   (`@<bp>.route(...)` for routes/*.py; `@app.route(...)` for
   gateway.py) live on thin wrapper functions in `__init__.py`.
2. **Wrapper bodies delegate to submodule implementations.**

   ```python
   # __init__.py
   from . import _consensus

   @pipelines_bp.route("/consensus/propose", methods=["POST"])
   def consensus_propose():
       return _consensus.consensus_propose()
   ```

This is the normalized shape **even where Flask Blueprints would
allow attaching `@<bp>.route(...)` directly inside a submodule**
(decision-8, refine feedback Q5). The trade-off:

- *Pro:* one place to look for the URL→handler map; routes still
  register on the Flask app object exactly as before; gateway and
  orchestrator are uniform.
- *Con:* one extra level of indirection per route.

The uniformity matters more than the indirection in a codebase where
multiple agents have to navigate the route surface in parallel.

## (g) When to further-split a submodule

If a submodule lands at-or-over the 1,500-line / 100-KB cap, **split
it further within the same slice**. The plan's slice descriptions
pre-allocate the canonical further-splits expected per cluster, e.g.
`_prompt_building/` is itself a sub-sub-package nested inside
`pipelines/`.

A fresh allowlist entry for "this new submodule is also over the cap"
is **not allowed** — it directly contradicts the
"allowlist empty" acceptance criterion of the parent issue (#2261).
If the further-split is non-obvious, escalate via a HITL
question rather than adding the entry.

The recursive shape (sub-package inside sub-package) is fine: the
barrel pattern composes. `pipelines/__init__.py` re-exports from
`pipelines/_pr_lifecycle/__init__.py`, which itself re-exports from
its own private submodules.

## (h) Follow-up issue convention

Decompositions are pure refactors. If a slice surfaces a latent bug
or a test-coverage gap, **file a follow-up issue** rather than
bundling the fix:

- Reference the parent issue (`Part of #2261`) in the follow-up.
- Note the slice that discovered the gap (`surfaced in slice-7
  during decomposition of orchestrator/overseer/monitor.py`).
- Don't bundle the fix into the slice's PR — bundling makes the
  slice harder to review, harder to revert, and breaks the "no
  behaviour change" acceptance criterion.

The same convention applies to test-logic rewrites that exceed
mechanical patch-path updates: the slice may fix a one-line
`patch("orchestrator.routes.pipelines._foo")` →
`patch("routes.pipelines._foo")` rewrite (feedback Q1), but
anything more invasive is a follow-up.

## Pre-merge checklist (per slice)

- [ ] Step-0 baseline commit lands cleanly (`git mv` only, no cluster
      extraction yet); `make test-all` green.
- [ ] Each cluster commit re-exports every external-referenced
      symbol through `__init__.py`.
- [ ] `git grep` audit (section (d)) ran for every cluster.
- [ ] No submodule lands over the 1,500-line / 100-KB cap; further
      splits are in-slice (section (g)).
- [ ] Allowlist entry for the file is removed (section (e)).
- [ ] Slice's CLAUDE.md seam table is updated with the new
      submodule layout (see `orchestrator/CLAUDE.md` and
      `gateway/CLAUDE.md`).
- [ ] `make lint` and `make test-all` are green.
- [ ] No behaviour change in the diff; behaviour-adjacent fixes are
      filed as follow-ups (section (h)).

## See also

- [Issue #2261](https://github.com/jwbron/egg/issues/2261) — full
  scope and acceptance contract for the decomposition program.
- [Issue #2248](https://github.com/jwbron/egg/issues/2248) and
  [#2250](https://github.com/jwbron/egg/issues/2250) — the file-size
  cap lint and allowlist this program drives down.
- [orchestrator/CLAUDE.md](../../orchestrator/CLAUDE.md) and
  [gateway/CLAUDE.md](../../gateway/CLAUDE.md) — submodule seam
  tables that downstream slices populate.
- [docs/architecture/slice-dag.md](../architecture/slice-dag.md) —
  the slice-DAG implement phase that drives the 15 slices in
  parallel.
