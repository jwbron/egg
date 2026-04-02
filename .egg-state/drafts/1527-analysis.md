### Task Analysis

**Problem statement**: When a tester (or other specialized agent) writes files outside its allowed scope, the gateway only blocks the push — by which point the agent has already wasted context window writing those files and then enters a retry loop trying to force-push, blocking BRC consensus.

**Source context**: Issue #1527, stemming from pipeline `issue-1522-v2`. The tester agent committed docs files alongside its test file. The gateway correctly rejected the push, but the agent burned remaining context trying workarounds (force push, rebase) instead of recovering.

**System context**: The guardrail architecture has three layers:
1. **Agent prompts** — `_build_file_boundary_section()` in `orchestrator/routes/pipelines.py:3785` already injects a "File Boundaries" section into agent prompts listing allowed/blocked patterns. The `concurrent_executor.py:140` also sets `EGG_AGENT_FILE_PATTERNS` env var. So prevention via prompt already exists (#1431).
2. **Gateway enforcement** — `gateway.py:907-937` calls `check_agent_restrictions()` at push time, returning a 403 with "Push denied: agent role '<role>' cannot modify these files." + the list of blocked files.
3. **Recovery** — This is the gap. The error message tells the agent what was blocked but not how to fix it.

**Technical root cause**: The gateway error at `gateway.py:929-937` returns a generic message with `blocked_files` in the JSON details, but:
- No remediation steps (e.g., "run `git reset HEAD~1`, re-add only allowed files, re-commit")
- No `egg-orch push --scope-filter` command exists to automatically strip out-of-scope files
- No mechanism to inject a recovery prompt into the agent's context after a push rejection

**Files affected**:
- `gateway/gateway.py:929-937` — Enrich the push-denied error message with specific remediation steps (CODER)
- `sandbox/agent-config/rules/push-recovery.md` — New rule file for push-rejection recovery instructions (DOCUMENTER — this is a .md file)
- Sandbox CLI tools — New `egg-orch push --scope-filter` subcommand (CODER)
- `gateway/tests/test_agent_restrictions_enforce.py` — Update tests for enriched error messages (TESTER — this is a test file)

**IMPORTANT role-scope note**: `.md` files are blocked for the coder role (`**/*.md` in blocked_patterns). Test files (`**/test_*.py`) are also blocked for the coder role. The coder must ONLY modify `gateway/gateway.py` and the new CLI push module. The documenter handles `.md` files. The tester handles test files. Do NOT attempt to create or modify files outside your role's allowed patterns.

**Risks / edge cases**:
- The prompt boundary section already exists (#1431) — the issue is about recovery not prevention. Must not duplicate or conflict with existing boundary injection.
- Error message format is consumed by agents parsing gateway responses — changing the format could affect existing retry logic if agents pattern-match on the current message.
- The `EGG_AGENT_FILE_PATTERNS` env var is already set by `concurrent_executor.py:149` — the scope-filter command can read this directly.
