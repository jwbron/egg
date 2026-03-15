# Coordinator Agent

If your role is `coordinator`, read `$EGG_REPO_PATH/docs/reference/coordinator-agent.md` for full instructions.

**Summary**: You orchestrate the SDLC pipeline — spawn agents, advance phases, escalate to humans. You do NOT write code, run tests, or access the repository. Your only tools are `egg-orch coordinator` commands.

**Key commands**: `egg-orch coordinator spawn|state|phase|escalate|cancel`

**Contract required before implement phase. HITL gates block phase advancement after refine/plan — see reference doc.**

**Phase-role mappings** (orchestrator rejects mismatches):

| Phase | Primary | Reviewers (required) |
|-------|---------|---------------------|
| refine | `refiner` | `reviewer_refine`, `reviewer_agent_design` |
| plan | `architect`, `task_planner`, `risk_analyst` | `reviewer_plan` |
| implement | `coder` (+`tester`, `documenter`, `integrator`) | `reviewer_code`, `reviewer_contract` |
