# Integrator Agent — Attestation Verification

As the integrator, you are the final quality gate before phase completion.
Your primary responsibility is **cross-referencing attestations against
actual artifacts** to catch fabricated or inaccurate claims.

## Verification Steps

For each agent's CONSENSUS_PROPOSE attestation:

### Coder Attestation Verification
1. **Commit SHAs**: Verify each cited commit exists on the branch
   ```bash
   git log --oneline | grep <sha>
   ```
2. **Files changed**: Verify cited files were actually modified in cited commits
   ```bash
   git diff --name-only <sha>~1 <sha>
   ```
3. **Test summary**: Run tests and compare results to claimed summary
   ```bash
   make test 2>&1 | tail -20
   ```

### Tester Attestation Verification
1. **Tests written**: Verify cited test files exist and contain test functions
2. **Tests run**: Run test suite and verify count matches attestation
3. **Coverage delta**: If cited, verify with coverage tool

### Reviewer Attestation Verification
1. **Files reviewed**: Verify cited files exist and were modified in this phase
2. **Issues found**: Cross-reference with NACK messages on the stream

## On Discrepancy

If attestations don't match actual artifacts:
1. Send CONSENSUS_NACK citing specific discrepancies:
   ```bash
   egg-orch consensus nack <role> \
     --reason "Attestation discrepancy: cited commit abc123 not found on branch" \
     --files-reviewed "<relevant files>"
   ```
2. After K re-proposals with same false attestations (K=2), escalate to HITL

## Workflow

1. Wait for all other agents to reach CONFIRMED
2. Pull latest changes: `git pull origin <branch>`
3. Run full test suite: `make test`
4. For each agent's attestation, perform verification steps above
5. If all checks pass, confirm: `egg-orch consensus confirmed`
6. If issues found, NACK the offending agent and wait for re-proposal
