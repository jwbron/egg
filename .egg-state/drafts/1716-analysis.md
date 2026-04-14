### Task Analysis

**Problem statement**: In BRC, reviewers send ACKs that carry no rationale because the CLI literally has no way to express one. Observed on pipeline issue-1707: `reviewer_code` rubber-stamped a coder proposal (5 empty-body ACKs across 3 producers) that `reviewer_contract` correctly NACKed. Proposals share a weaker form of the same problem: producers pack everything into a free-form `summary` with no structured slots for commit SHA, files changed, tests run, or tasks satisfied.

**Source context**: Issue #1716, first of three sibling issues (#1717 persistence, #1718 cross-agent structured messaging). Framed as a CLI/protocol asymmetry fix, not an agent-discipline problem — the content bar has to be enforceable at the protocol boundary. Replaces #1712 (split for scope). Can land first and produces better content in Redis immediately, even before persistence improves.

**Workarounds**: None known — there is no way to attach rationale to an ACK today. Reviewers who want reasoning on record must NACK.

**System context**: BRC consensus is driven through CLI entry points in `sandbox/bin/egg-orch` that POST signals to the orchestrator:
- `cmd_consensus_propose` (egg-orch:1225) → `handle_consensus_propose_signal` (signals.py ~895)
- `cmd_consensus_ack` (egg-orch:1296) → `handle_consensus_ack_signal` (signals.py:991)
- `cmd_consensus_nack` (egg-orch:1323) → `handle_consensus_nack_signal` (signals.py:1071)
- `cmd_consensus_withdraw` (egg-orch:1351) → `handle_consensus_withdraw_signal` (signals.py:1127)

Each handler writes a `Message` to the message store. Proposals write `body=payload.get("summary","")`, ACKs write `body=payload.get("reason","")`, NACKs write `body=payload.get("reason","")`, withdraws write `body=reason`. Agents learn these commands from the BRC protocol block rendered by `_build_brc_protocol_block` in `orchestrator/routes/pipelines.py` (producer lifecycle ~5330, reviewer lifecycle ~5352).

**Technical root cause**:
1. **ACK**: The argparser at `sandbox/bin/egg-orch:1837-1849` accepts only `--files-reviewed`; does not expose `--reason`. `cmd_consensus_ack` (1296-1320) builds `payload = {"artifact_references": args.files_reviewed}` — no reason key, so `handle_consensus_ack_signal` at `signals.py:1034` resolves `payload.get("reason","")` to `""` every time. ACK messages are guaranteed-empty by construction.
2. **PROPOSE**: The argparser (egg-orch:1811-1834) accepts `--summary`, `--artifacts`, `--risk`, `--commit-sha`, `--changed-artifacts`, `--push`. `cmd_consensus_propose` (1261-1267) packs these into `{summary, attestation: {}, artifacts, risk_considered, commit_sha}`. Nothing for tests-run or tasks-satisfied; `summary` is free-form prose with no structured sections. `handle_consensus_propose_signal` writes `body=payload.get("summary","")` (signals.py:942) — the rest survives in `metadata.payload` but is invisible to consumers reading `body`.
3. **No minimum-content floor**: None of the four BRC handlers reject empty/short content. (`signals.py` has an attestation `checks_passed` guard ~line 808; separate concern, no analogue for rationale length.)
4. **Reviewer prompt** at `orchestrator/routes/pipelines.py:5366-5367` mirrors the CLI: `egg-orch consensus ack <role> --files-reviewed "f1" "f2"` with no slot for a reason. Producer prompt at 5337 likewise has no structured commit/tests/tasks breakdown.

**Files affected**:
- `sandbox/bin/egg-orch` — (a) add `--reason` (required) to `ack` parser; (b) thread it through `cmd_consensus_ack` into `payload["reason"]`; (c) add `--commit`, `--files-changed`, `--tests-run`, `--tasks` to `propose` parser and fold into payload in `cmd_consensus_propose`.
- `orchestrator/routes/signals.py` — add `_validate_brc_content` helper and invoke from `handle_consensus_propose_signal`, `handle_consensus_ack_signal`, `handle_consensus_nack_signal`, `handle_consensus_withdraw_signal`. Returns 400 with actionable message when content is missing, <50 chars, or matches boilerplate.
- `orchestrator/routes/pipelines.py` — update reviewer/producer lifecycle text in `_build_brc_protocol_block` (lines 5330–5377) to describe new required args and what substantive content looks like.
- Tests: `sandbox/tests/` for new CLI flags; `orchestrator/tests/` for content-validation behavior and the four BRC handlers.

**Risks / edge cases**:
- **Backward-compat mid-flight**: Making `--reason` required on `ack` breaks any running agent. Mitigation: agents are stateless per run; new pipelines use new prompts + new CLI. Error messages must be loud so stragglers fail visibly, not silently.
- **Boilerplate false positives**: Too-aggressive regex could reject legitimate short rationales. Use length floor (≥50 chars) + small case-insensitive exact-match boilerplate list, both tunable via a module constant.
- **Withdraw is symmetric**: `--reason` already required at CLI (egg-orch:1870). Minimum-content floor must still apply.
- **Re-propose flow**: `--changed-artifacts` triggers re-propose (signals.py:924). Content bar must apply on re-propose — tests must cover this.
- **Scope boundary**: Issue step 6 (overseer anomaly detection, post-consensus quality gate) is explicitly follow-on; belongs to #1717/#1718 and must NOT land in this PR.