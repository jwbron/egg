## Analysis — Issue #1718

### Problem
During BRC consensus, agents have no clear structured path to coordinate with peers. The `egg-orch message send` CLI exists but the BRC preamble never tells agents to use it.

### Files affected
- `orchestrator/routes/pipelines.py` — extend `_build_brc_preamble` (~5251) with directed coordination guidance
- `sandbox/egg_lib/orch_cli.py:1782` — update `--type` help text to include `HANDOFF`
- `docs/guides/concurrent-execution.md` — add directed coordination subsection with worked example
- `orchestrator/tests/test_pipeline_prompts.py` — add test asserting BRC preamble contains new guidance