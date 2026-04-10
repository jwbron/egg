### Task Analysis

**Problem statement**: In concurrent/BRC mode, agents can bypass the consensus protocol by calling `git push` directly instead of `egg-orch consensus propose --push`. This means changes can land on the branch without going through peer review, breaking the "all changes must be reviewed" invariant.

**Source context**: Issue #1669 was created after observing pipeline #1570 v17, where the coder agent pushed 7 incremental commits but never entered BRC — no `CONSENSUS_PROPOSE` messages despite multiple pushes. The auto-repropose mechanism from #1666/#1667 didn't trigger, possibly because the coder never updated the contract. The issue proposes hard enforcement at the gateway level rather than relying on agent compliance.

**Workarounds**: The existing auto-repropose on push (#1666/#1667) provides a safety net — if an agent pushes without proposing, the orchestrator can auto-repropose. But this depends on contract state and has debounce windows, making it unreliable as a sole enforcement mechanism.

**System context**: All `git push` operations from agents flow through the git wrapper script (`sandbox/bin/git:204-295`) which sends an HTTP POST to the gateway's `/api/v1/git/push` endpoint (`gateway/gateway.py:643`). The gateway validates branch ownership, file restrictions, and push targets, then executes the push. The `egg-orch consensus propose --push` command (`sandbox/egg_lib/orch_cli.py:1111-1190`) also calls `git push` (which goes through the same wrapper→gateway path), then sends a `CONSENSUS_PROPOSE` signal to the orchestrator. Currently, the gateway cannot distinguish these two push paths — no marker differentiates a consensus-protocol push from a direct push.

**Technical root cause**: The gateway's `git_push()` handler has no concurrent-mode awareness. It enforces branch ownership, file restrictions, and push targets, but doesn't check whether the push is part of the BRC consensus protocol. The `EGG_CONCURRENT_MODE` env var is available on the gateway (already used in `checkpoint_handler.py:153`) but not consulted during push validation.

**Files affected**:
- `gateway/gateway.py` — Add concurrent mode push check in `git_push()` after push target enforcement (~line 814). Block pushes that don't have a `consensus_push` marker.
- `sandbox/bin/git` — Pass `EGG_CONSENSUS_PUSH` env var through as `consensus_push` field in the JSON payload to the gateway.
- `sandbox/egg_lib/orch_cli.py` — Set `EGG_CONSENSUS_PUSH=1` in the subprocess environment when `cmd_consensus_propose()` runs `git push`.
- `gateway/tests/` — New test file for concurrent mode push blocking.
- `sandbox/agent-config/rules/mission.md` — Update wording to reflect that direct push is now blocked (not just recommended against).

**Risks / edge cases**:
- Infrastructure pushes (checkpoints, pipeline state branches) are already exempt via the `is_infrastructure_push` check at line 732 — the new check must go AFTER this exemption to avoid blocking internal operations.
- Non-concurrent pipelines are unaffected — the check only activates when `EGG_CONCURRENT_MODE=true`.
- Agents could theoretically bypass by setting `EGG_CONSENSUS_PUSH=1` in their own environment before calling `git push`. This is a soft bypass risk, but acceptable — the goal is structural enforcement against accidental misuse, not cryptographic prevention.
- Killswitch needed — following the `PUSH_TARGET_ENFORCEMENT` pattern, add a `CONCURRENT_PUSH_ENFORCEMENT` env var to disable in emergencies.