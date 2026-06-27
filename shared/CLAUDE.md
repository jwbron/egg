# Shared Libraries

Reusable Python libraries shared between the gateway sidecar and the sandbox container (`egg_agent`, `egg_contracts`, `egg_orchestrator`, `egg_restrictions`, and friends).

- **[README.md](README.md)** — package inventory and usage examples
- **[../docs/index.md](../docs/index.md)** — full documentation index
- **[../CLAUDE.md](../CLAUDE.md)** — repo-level quick reference and layout

## Testing

Run `make test` from the repo root — it's changeset-aware and selects only the tests reachable from your diff. `make test-all` runs the full suite. See [docs/guides/testing.md](../docs/guides/testing.md) and the root [CLAUDE.md](../CLAUDE.md#quick-reference). Avoid invoking `pytest` directly: you'll skip the narrowing and may hit venv-PATH issues.

## Decomposition seams

Oversized `shared/` modules are split into **sub-packages with an explicit re-export barrel**, per the canonical [decomposition pattern](../docs/guides/decomposition-pattern.md) ([#3312](https://github.com/jwbron/egg/issues/3312)). The `__init__.py` barrel is the **stable public API**: external importers and `unittest.mock.patch` targets resolve through it (`from egg_contracts.plan_parser import X`, `patch("egg_contracts.plan_parser._foo")`), so they survive the split. Submodules are underscore-prefixed and package-private.

### `egg_contracts/plan_parser/` — plan-document → contract-task parser ([#3312](https://github.com/jwbron/egg/issues/3312), slice 7)

`plan_parser.py` (1,952 lines) → `egg_contracts/plan_parser/` (largest submodule `_yaml_parse.py`, 832 lines). Three-tier plan parser (YAML code-fence → YAML front-matter → markdown-regex fallback) plus the forest/overlap/role/pre-flight validators. The barrel does explicit per-symbol re-exports and declares `__all__` (a superset of the pre-split module's original 11-name `__all__`, extended with the module-global symbols external code and tests reference by name), preserving the full public API.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel) | Stable public API: per-symbol re-exports + `__all__`; module-doc on the parsing strategy | `parse_plan`, `parse_plan_file`, `validate_plan_preflight` (+ re-exports of every symbol below) |
| `_models.py` | Dataclasses, `PlanPreflightError`, Jira/slice constants, compiled regex patterns | `ParsedTask`, `ParsedPhase`, `ParseResult`, `ParseWarning`, `PlanPreflightError`, `TASK_PATTERN`, `YAML_FENCE_PATTERN`, `PHASE_HEADER_PATTERN`, `GOAL_PATTERN`, `FILES_PATTERN`, `PLACEHOLDER_ACCEPTANCE_CRITERIA`, `JIRA_ACTION_VALUES`, `JIRA_ACTION_STATUS_VALUES`, `_JIRA_KEY_PATTERN`, `_KNOWN_SLICE_KEYS` |
| `_yaml_parse.py` (largest, 832 lines) | `# yaml-tasks` fence + front-matter + per-task / per-slice / `pr` extraction | `parse_yaml_code_fence`, `parse_yaml_frontmatter`, `parse_tasks_from_yaml`, `parse_phases_from_yaml`, `extract_pr_metadata_from_yaml`, `_extract_jira_task_fields`, `_normalize_optional_string`, `_line_value_is_none` |
| `_markdown_parse.py` | Fragile `[TASK-{phase}-{number}]` markdown-regex fallback extraction | `parse_tasks_from_markdown`, `parse_phases_from_markdown` |
| `_orchestration.py` | `parse_plan` / `parse_plan_file` three-tier driver + comment rendering | `parse_plan`, `parse_plan_file`, `format_warnings_for_comment` |
| `_validators.py` | Forest/cycle (#2137), file-overlap (#3046), role↔files (#2527), pre-flight (#2777) validators | `validate_forest`, `validate_slice_file_overlap`, `validate_task_role_alignment`, `validate_plan_preflight`, `_detect_cycles`, `_check_role_files`, `_eligible_producer_roles`, `_is_file_blocked_for_role` |

Pure refactor: every re-exported symbol is AST-identical to the pre-split file. The one intentional seam edit is `validate_plan_preflight`, which calls `parse_plan` **through the package module object** (`import egg_contracts.plan_parser as _pkg`) so the pre-split module-global seam `patch("egg_contracts.plan_parser.parse_plan")` keeps intercepting it. Module-level `patch("egg_contracts.plan_parser._foo")` targets resolve through the barrel; tests that patch a helper **where it is called** target the caller's submodule. `_yaml_parse.py` (832 lines) trips only the 800-line soft advisory — left whole to keep the YAML-extraction seam cohesive ([decomposition pattern §g](../docs/guides/decomposition-pattern.md)).

This is the first `shared/` decomposition; later `shared/` slices append their own rows to this table.
