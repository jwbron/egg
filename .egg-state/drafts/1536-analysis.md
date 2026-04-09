### Task Analysis

**Problem statement**: When a pipeline runs the implement phase, agents get assigned tasks involving files they can't push. The coder gets stuck in a push-retry loop on `.md` and `test_*.py` files, blocking the entire pipeline — tester and documenter sit idle.

**Source context**: Issue #1536, reported after pipeline `issue-1527` wasted ~60 minutes. The coder was assigned `sandbox/agent-config/rules/push-recovery.md` (blocked by `**/*.md`) and `gateway/tests/test_agent_restrictions_enforce.py` (blocked by `**/test_*.py`). These should have gone to documenter and tester respectively.

**System context**: The plan generation flow (both remote task_planner and local short-flow S3) produces a `yaml-tasks` appendix with tasks containing `id`, `description`, `acceptance`, and `files`. Tasks have no `role` field — there's no way to indicate which agent should own a task. During the implement phase, `_build_role_context()` (`pipelines.py:2578`) feeds ALL phase tasks to ALL execution agents (coder, tester, documenter). Each agent has file boundaries via `_build_file_boundary_section()`, but the agent only learns it can't push after it's already done the work. Meanwhile, `AGENT_PATTERNS` in `egg_restrictions/patterns.py` and `AGENT_ROLES` in `egg_contracts/agent_roles.py` define clear file access rules per role — this data exists but isn't used during plan generation.

**Technical root cause**: Two gaps: (1) The task_planner prompt (`pipelines.py:5288-5351`) has no information about agent role file restrictions, so it assigns all tasks to a single "implement" phase without role awareness. (2) The yaml-tasks schema, `Task` model, and `plan_parser.py` have no `role` field, so even if the planner assigned roles, the pipeline couldn't route tasks to the correct agent.

**Files affected**:
- `.egg/schemas/yaml-tasks.schema.json` — add optional `role` field to task definition
- `shared/egg_contracts/models.py` — add optional `role` field to `Task` model
- `shared/egg_contracts/plan_parser.py` — parse `role` from yaml-tasks, pass through to `ParsedTask` → `Task`
- `orchestrator/routes/pipelines.py` — (a) inject role file restrictions into task_planner prompt (~line 5288), (b) filter tasks by `role` in `_build_role_context()` for implement-phase execution agents
- `orchestrator/tests/test_pipeline_prompts.py` — test the new role restriction section in task_planner prompt
- `tests/shared/egg_contracts/test_plan_parser.py` — test `role` field parsing

**Risks / edge cases**:
- Backward compatibility: `role` must be optional — existing plans without role fields must still parse correctly
- Unassigned tasks (role=None) default to coder as fallback
- Cross-role tasks need splitting into separate tasks per role