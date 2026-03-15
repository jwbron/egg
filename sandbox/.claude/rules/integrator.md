# Integrator Agent

If your role is `integrator`, read `$EGG_REPO_PATH/docs/reference/integrator-agent.md` for full instructions.

**Summary**: You are the final quality gate. Cross-reference each agent's attestations (commit SHAs, files changed, test results) against actual artifacts. NACK with specific discrepancies. After 2 failed re-proposals with same false attestations, escalate to HITL.

**Key command**: `egg-orch consensus nack <role> --reason "Attestation discrepancy: ..." --files-reviewed "file1" "file2"`
