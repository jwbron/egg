## Task Analysis — Issue #1715

**Problem statement**: `egg-checkpoint` has five distinct usability papercuts that caused the reporter to burn ~6 failed invocations before getting useful output when investigating pipeline `issue-1707`. Individually small, collectively they make the tool feel like a guessing game.

**Source context**: GitHub issue jwbron/egg#1715, reporter self-filed during investigation of #1712 (empty reviewer ACKs in pipeline issue-1707). Clear, actionable report with concrete command/output examples and suggested fixes. Five concrete frictions listed in priority order.

**Workarounds**: Users reach for `EGG_CHECKPOINT_REPO=...` env var when the flag fails, never discovering the top-level flag. For agent-type filter, users have to translate `reviewer_code` → `reviewer` manually. For `--json` empty, scripts must special-case empty stdout.

**System context**: `egg-checkpoint` is a CLI for browsing agent session checkpoints stored on the `egg/checkpoints/v2` branch (see `CHECKPOINT_BRANCH` in `egg_config.constants`). It runs either directly via git (reading `.checkpoints/` dir on the checkpoint branch) or through the gateway HTTP API (`/api/v1/checkpoints`). Parser and commands live in `shared/egg_contracts/checkpoint_cli.py`; `sandbox/egg_lib/checkpoint_cli.py` is a re-export. Agent role mapping to the coarser `AgentType` enum happens in `gateway/checkpoint_handler.py:219-233` (`_ROLE_TO_AGENT_TYPE`); the `AgentType` enum lives in `shared/egg_contracts/checkpoints.py:158`. The full `SessionMetadata.agent_role` string is preserved inside each checkpoint (see `checkpoints.py:49`), but the `CheckpointSummaryV2` (checkpoints.py:314-348) and its index store only the coarse `AgentType`, so fast-path filtering in `filter_checkpoints_v2` (checkpoint_loader.py:386) cannot use the raw role directly.

**Technical root causes**:

1. **`--checkpoint-repo` position**: It's added only to the top-level `ArgumentParser` (checkpoint_cli.py:1651). argparse does not accept top-level options after the subcommand name, so `egg-checkpoint list --checkpoint-repo ...` fails with "unrecognized arguments." No shared parent parser is used.
2. **Silent empty results**: When `ensure_checkpoint_ref` returns a ref but filters match nothing (cli:773-776) or when the index is empty (cli:748-756), the error messages don't reveal which repo/branch was actually searched. `_print_repo_hint` only fires when `checkpoint_repo` is None.
3. **`--agent-type` collapse**: Parser uses `choices=[a.value for a in AgentType]` (cli:1677, 1713, 1765) which only contains `reviewer`, not `reviewer_code`/`reviewer_contract`/etc. `_ROLE_TO_AGENT_TYPE` maps them all to `AgentType.REVIEWER` on ingest. The raw role is preserved in `SessionMetadata.agent_role` per-checkpoint but not in the summary index.
4. **`--json` on empty**: All "no checkpoints" branches print plain text and return 0 regardless of `args.json` (cli:719, 748, 754, 774, 823, 867, 913, 919, 931, and more). Consumers receive text on stdout and a parsing error.
5. **Silent missing roles**: `reviewer_code`/`reviewer_contract` never produced checkpoints for `issue-1707`. The CLI has no docs clarifying that reviewer agents may not produce checkpoints (trigger pattern depends on session-end behaviour); help text doesn't mention this.

**Files affected**:
- `shared/egg_contracts/checkpoint_cli.py` — all five fixes land here
- `tests/shared/egg_contracts/test_checkpoint_cli.py` — new tests for each fix
- `tests/shared/egg_contracts/test_checkpoint_cli_http.py` — HTTP empty-result JSON handling if changed

**Risks / edge cases**:
- `--checkpoint-repo` after subcommand: use a shared parent parser passed via `parents=[common_parser]` so both positions resolve to the same `Namespace` attribute. argparse last-wins semantics handle conflicts naturally.
- For composite role names: filter at the index level by `AgentType.REVIEWER` (fast), then post-filter by loading each `CheckpointV2` and checking `session.agent_role`. Cost: one full-checkpoint load per reviewer summary in the result set — bounded and acceptable.
- `--json` empty must still exit 0 and emit valid JSON. `list`/`browse`/`search` emit `[]`; `context`/`cost` emit structured empty payloads matching their non-empty shape.
- Empty-result informational line goes to stderr so `--json` stdout stays pure.
- HTTP path: the gateway collapses roles server-side, so composite-role filtering for `--agent-type` works only in the direct-git path. Document this limitation in the help text.
- Documentation (#5) is a help-text-only change; no behavior impact.