# Plan: egg-checkpoint CLI papercut fixes (#1715)

## Summary

Address the five concrete `egg-checkpoint` CLI papercuts reported in jwbron/egg#1715. All fixes land in `shared/egg_contracts/checkpoint_cli.py`; the sandbox CLI is a re-export, so no additional edits are needed there. Strategy: (1) extract a shared parent parser so `--checkpoint-repo` and `--repo-path` are accepted both before and after the subcommand — the simplest argparse idiom for this; (2) factor empty-result rendering into a single helper that knows the checkpoint repo + branch that were searched and respects `--json` by emitting `[]` (or the equivalent structured empty payload for context/cost), with the informational "Searched X branch Y" line going to stderr so stdout remains valid JSON; (3) accept the full set of BRC composite role names in `--agent-type` choices, collapse them to `AgentType.REVIEWER` for the fast index lookup, then post-filter the resulting summaries by loading each `CheckpointV2` and matching `SessionMetadata.agent_role` — cost is acceptable since REVIEWER-tagged subsets are bounded per pipeline; (4) add a note to the relevant `--help` strings that reviewer agents may not produce checkpoints if session-end triggers don't fire, and that composite role filtering is a direct-git-path feature.

**Risks / edge cases**: Parent parser precedence uses argparse last-wins for `--checkpoint-repo` if supplied in both positions. JSON empty must be valid JSON — `list`/`browse`/`search` emit `[]`; `context`/`cost` emit schema-shaped empty payloads. Composite role post-filter is O(N) git reads on the REVIEWER subset — acceptable for typical pipelines. Gateway HTTP path collapses roles server-side, so composite-role filtering works only in the direct-git path; document this limitation in help. Info line always goes to stderr even with `--json`.

## Implementation

### Phase 1: Implement

Single phase — five small, independent fixes bundled into one PR.

**Tasks**:
1. **[task-1-1]** Refactor `create_parser()` in `shared/egg_contracts/checkpoint_cli.py:1641` to add `--checkpoint-repo` and `--repo-path` to a shared parent `ArgumentParser(add_help=False)`. Pass it via `parents=[common_parser]` to every `subparsers.add_parser(...)` call so the flags are accepted before or after the subcommand. Add tests that cover both positions and the both-supplied conflict (argparse last-wins).
2. **[task-1-2]** Add a helper `_print_empty_result(checkpoint_repo, branch, json_mode, stream=sys.stderr, shape="list")` and route all "No checkpoints found..." branches in `cmd_list`, `cmd_browse`, `cmd_search`, `cmd_context`, `cmd_cost`, and their `_cmd_*_http` counterparts through it. Helper must (a) print `Searched <repo> branch <branch>` to stderr, (b) still emit the existing hint when appropriate, and (c) when `json_mode` is True, print the shape-appropriate empty JSON payload to stdout (`[]` for list-shaped commands; structured empty object for `context`/`cost`).
3. **[task-1-3]** In `create_parser()`, extend `--agent-type` choices for `list`, `context`, and `search` subparsers to include composite BRC role names (`reviewer_code`, `reviewer_contract`, `reviewer_agent_design`, `reviewer_refine`, `reviewer_plan`). In each `cmd_*` function, when `args.agent_type` is a composite reviewer name: (a) pass `AgentType.REVIEWER.value` to `filter_checkpoints_v2`, (b) after filtering, load each summary's full `CheckpointV2` via `load_checkpoint_from_ref` and keep only those whose `session.agent_role` equals the composite name. Fall through unchanged for non-composite values. Note the HTTP-path limitation in the `--agent-type` help string.
4. **[task-1-4]** In task-1-2's helper, ensure `--json` empty output is always valid parseable JSON. Add test cases that call each command with an empty result and `--json`, run `json.loads(stdout)`, and assert it succeeds and produces the expected empty shape. Exit code must remain 0.
5. **[task-1-5]** Update the top-level parser description and the `--agent-type` help strings to note that (a) reviewer agents may not produce checkpoints if session-end triggers don't fire, and (b) composite BRC roles are supported in the direct-git path but collapse to `reviewer` when querying via the gateway. Update the doc comment at the top of the module if necessary.

```yaml
# yaml-tasks
pr:
  title: "egg-checkpoint: fix five CLI papercuts from #1715"
  description: |
    Addresses the five concrete `egg-checkpoint` usability issues from #1715:
    flag positioning, silent empty results, composite BRC role filtering,
    invalid JSON on empty results, and missing docs about reviewer checkpoints.
    All fixes land in `shared/egg_contracts/checkpoint_cli.py`.
  test_plan: |
    - Automated: new unit tests in `tests/shared/egg_contracts/test_checkpoint_cli.py` covering each fix (both flag positions, empty-result stderr line, `--json` empty emits valid JSON, composite reviewer filter returns expected rows, help text mentions caveats).
    - Manual: run `egg-checkpoint --checkpoint-repo jwbron/egg-checkpoints list --issue 1707`, `egg-checkpoint list --checkpoint-repo jwbron/egg-checkpoints --issue 1707`, `egg-checkpoint list --agent-type reviewer_code --issue 1707`, and `egg-checkpoint list --agent-type reviewer --issue 9999 --json | jq .` to confirm each papercut is resolved.
  manual_steps: |
    Pre-merge: none.
    Post-merge: none.
phases:
  - id: 1
    name: Implement
    goal: "Ship all five #1715 papercut fixes in one PR with tests."
    tasks:
      - id: task-1-1
        description: "In `shared/egg_contracts/checkpoint_cli.py:create_parser()`, build a shared `ArgumentParser(add_help=False)` that declares `--checkpoint-repo` and `--repo-path`, and pass it via `parents=[...]` to every `subparsers.add_parser(...)` call so both flags are accepted before or after the subcommand name. Ensure the top-level parser still accepts them too — argparse last-wins resolves conflicts."
        acceptance: "`egg-checkpoint --checkpoint-repo X list --issue 42` and `egg-checkpoint list --checkpoint-repo X --issue 42` both succeed and resolve the same checkpoint_repo; a test exercises both positions and the both-supplied case."
        files:
          - shared/egg_contracts/checkpoint_cli.py
          - tests/shared/egg_contracts/test_checkpoint_cli.py
      - id: task-1-2
        description: "Add `_print_empty_result(checkpoint_repo, branch, json_mode, shape)` to `shared/egg_contracts/checkpoint_cli.py` and replace every `print('No checkpoints found...')` branch in `cmd_list`, `cmd_browse`, `cmd_search`, `cmd_context`, `cmd_cost`, and their `_cmd_*_http` siblings. The helper prints `Searched <repo> branch <branch>` to stderr, keeps the existing repo-hint behavior, and when `json_mode` is True prints the shape-appropriate empty JSON to stdout (`[]` for list-shaped commands; structured empty object for context/cost)."
        acceptance: "Every empty-result path prints the informational line to stderr; when `--json` is set, stdout is parseable via `json.loads` and matches the expected empty shape; exit code stays 0."
        files:
          - shared/egg_contracts/checkpoint_cli.py
          - tests/shared/egg_contracts/test_checkpoint_cli.py
      - id: task-1-3
        description: "In `shared/egg_contracts/checkpoint_cli.py:create_parser()`, extend the `--agent-type` choices for the `list`, `context`, and `search` subparsers to include `reviewer_code`, `reviewer_contract`, `reviewer_agent_design`, `reviewer_refine`, `reviewer_plan`. In the `cmd_list`, `cmd_context`, and `cmd_search` functions, when `args.agent_type` is a composite reviewer name: (a) call `filter_checkpoints_v2(agent_type=AgentType.REVIEWER.value, ...)`, (b) for each resulting summary load the full `CheckpointV2` via the existing `load_checkpoint_from_ref` pathway and keep only entries where `session.agent_role == args.agent_type`. Non-composite values fall through unchanged. Update the `--agent-type` help to note the HTTP-path limitation."
        acceptance: "`egg-checkpoint list --agent-type reviewer_code --issue 1707` returns only checkpoints whose session metadata records `agent_role == 'reviewer_code'`; unknown roles still produce an argparse error; help text mentions the gateway-path limitation."
        files:
          - shared/egg_contracts/checkpoint_cli.py
          - tests/shared/egg_contracts/test_checkpoint_cli.py
      - id: task-1-4
        description: "Extend `tests/shared/egg_contracts/test_checkpoint_cli.py` (and `test_checkpoint_cli_http.py` as needed) to verify `--json` empty output for every command (list, browse, search, context, cost) is valid parseable JSON. Call `json.loads(stdout)` and assert the empty shape (`[]` or structured object) and exit code 0."
        acceptance: "All `--json` empty-result tests pass; `json.loads` on stdout never raises for any command; the informational line appears on stderr."
        files:
          - tests/shared/egg_contracts/test_checkpoint_cli.py
          - tests/shared/egg_contracts/test_checkpoint_cli_http.py
      - id: task-1-5
        description: "Update the top-level parser description in `create_parser()` and the `--agent-type` help strings in the list, context, and search subparsers to state: (a) reviewer agents may not produce checkpoints if session-end triggers don't fire, and (b) composite BRC role values are supported via the direct-git path but collapse to `reviewer` when queried via the gateway HTTP API. Adjust the module-level docstring at the top of `checkpoint_cli.py` if needed."
        acceptance: "`egg-checkpoint --help` and `egg-checkpoint list --help` mention the reviewer-trigger caveat; `egg-checkpoint list --help` mentions the gateway composite-role caveat."
        files:
          - shared/egg_contracts/checkpoint_cli.py
```
