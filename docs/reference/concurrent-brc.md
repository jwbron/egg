# BRC Consensus Protocol (Agent Reference)

This is the quick-reference for agents running in concurrent execution mode (`EGG_CONCURRENT_MODE=true`). For the full guide, see `docs/guides/concurrent-execution.md`.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `EGG_CONCURRENT_MODE` | `true` when running in concurrent execution mode |
| `EGG_MESSAGE_POLL_INTERVAL` | Suggested polling interval in seconds (default: 30) |
| `EGG_BRC_ROLE_TYPE` | Your role type: `producer`, `reviewer`, or `producer,reviewer` |
| `EGG_BRC_REVIEWERS` | Comma-separated reviewer roles assigned to review your work (producers) |
| `EGG_BRC_PRODUCERS` | Comma-separated producer roles you are assigned to review (reviewers) |

## Message Polling

Use long-polling instead of sleep loops:
```bash
egg-orch message poll --wait 30  # Blocks until messages arrive (~1s delivery)
```

## Producer Workflow (coder, tester, documenter)

1. **Do your work** — implement, test, or document as assigned
2. **Propose** when done:
   ```bash
   egg-orch consensus propose --summary "Implemented feature X" \
     --artifacts "src/auth.py" "src/auth_test.py" \
     --risk "Rate limiting not yet implemented"
   ```
3. **Wait for reviews** — poll for ACK/NACK messages from reviewers
4. **Handle NACKs** — address concern, then re-propose with `--changed-artifacts`
5. **Confirm** when all reviewers have ACKed: `egg-orch consensus confirmed`
6. **Stay alive** — keep polling. The orchestrator sends SIGTERM when all agents confirm.

**Attestation requirements by role:**

| Role | Required in proposal |
|------|---------------------|
| **Coder** | commit SHAs, files changed, test pass/fail summary, one risk considered |
| **Tester** | tests written/run count, coverage delta, edge cases covered, one concern |
| **Documenter** | sections updated, links verified, one concern considered |

## Reviewer Workflow (reviewer_code, reviewer_contract, checker)

1. **Detect new commits** from your assigned producers (check `EGG_BRC_PRODUCERS`)
2. **Form independent judgment** from git artifacts — review actual code, don't wait for the producer's self-assessment
3. **ACK or NACK** each assigned producer:
   ```bash
   egg-orch consensus ack coder --files-reviewed "src/auth.py" "src/utils.py" \
     --summary "Code correct, tests pass"

   egg-orch consensus nack coder --reason "SQL injection in auth.py:42" \
     --files-reviewed "src/auth.py"
   ```
4. **Confirm** when all assigned producers have been reviewed and ACKed: `egg-orch consensus confirmed`
5. **Stay alive** — keep polling for re-proposals if you NACKed.

**Attestation requirements by role:**

| Role | Required in ACK/NACK |
|------|---------------------|
| **Reviewer (code)** | files reviewed (paths), issues found/resolved count, one risk |
| **Reviewer (contract)** | tasks verified (IDs), acceptance criteria checked, gaps |
| **Checker** | lint/type/test results, auto-fixes applied, remaining warnings |

## Anti-Sycophancy Requirements

- **ACKs must cite specific artifacts** — file paths, line numbers, commit SHAs. Not just "looks good."
- **Reviewers must identify at least one concern** — or explicitly reason about why there are none.
- **Form independent judgments** before seeing producer self-assessments.
- **NACKs must be specific and actionable** — cite the exact issue and what needs to change.

## Tester Dual Role

The tester is both a **producer** (proposes test artifacts) and a **reviewer** (evaluates coder's work by running tests). Both must reach CONFIRMED for the tester to be fully confirmed.

## Handling Agent Failures

| Failed Agent | Other agents should... |
|-------------|----------------------|
| Coder | Continue waiting |
| Tester | Coder/documenter continue; integrator notes the gap |
| Reviewer | Coder continues; integrator notes review gap |
| Integrator | All agents signal BLOCKED |
