# Release note — `max_parallel_slices` default lowered 2 → 1 (#2904)

**Issue:** [#2904](https://github.com/jwbron/egg/pull/2904) — make per-pipeline `max_parallel_slices` configurable at pipeline creation; lower the fallback default from 2 to 1 so small/single-node hosts don't crash under implicit parallelism.

## What changed

1. **New `PipelineConfig.max_parallel_slices` field** (`int | None`,
   default `None`, `ge=1`). Settable at pipeline creation through
   `submit_task`'s `config` payload, e.g.
   `{ "max_parallel_slices": 3 }` on a host that can handle 3 × ~8 ≈ 24
   concurrent agent containers.

2. **The implement-phase slice loop honours the field.**
   `_run_implement_phase_slices` now passes
   `pipeline.config.max_parallel_slices` into the `SliceScheduler`
   constructor. Precedence:

   1. Per-pipeline `PipelineConfig.max_parallel_slices` (when non-`None`).
   2. `EGG_ORCH_MAX_PARALLEL_SLICES` env var.
   3. Built-in default (now `1`).

3. **Default lowered 2 → 1.** `DEFAULT_MAX_PARALLEL_SLICES` is now
   `1`, so the effective default is a single slice per wave unless
   the operator raises it (per-pipeline or via env).

4. **Process-wide `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` is unchanged.**
   Default still `4`.

## Why

A slice-DAG implement-phase wave spawns one BRC team (~8 containers)
per slice. The prior per-pipeline default of 2 (~16 containers +
overseers) saturated a single-node k3s host mid-run, sending the node
NodeNotReady and permanently killing a producer pod — pipeline
failed. See [#2806](https://github.com/jwbron/egg/issues/2806). The
new default optimises for small/single-node clusters; operators on
bigger hosts opt-in to higher concurrency.

## Operator-visible behaviour change

Pipelines that previously relied on the implicit `2` will now
**serialize their slice waves** unless:

- the pipeline config sets `max_parallel_slices` explicitly at
  creation, or
- `EGG_ORCH_MAX_PARALLEL_SLICES` is set to ≥ 2 on the orchestrator.

For most hosts this is the safe new default; for hosts with capacity,
prefer the per-pipeline knob (it's per-pipeline rather than
process-wide and survives orchestrator restarts via the contract).

## Upgrade

No migration needed — the field is optional and stored configs
deserialize unchanged. To preserve the prior `2`-slice cadence after
upgrade, either:

```jsonc
// submit_task config — preferred, per-pipeline
{ "max_parallel_slices": 2 }
```

or set the env var on the orchestrator:

```bash
export EGG_ORCH_MAX_PARALLEL_SLICES=2
```

— Authored by egg
