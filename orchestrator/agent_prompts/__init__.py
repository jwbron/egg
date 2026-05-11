"""Per-role agent prompt strings used by the orchestrator.

Lives as a Python package (not as ``.md`` files under
``shared/prompts/``) so the coder role can author the prompt without
clashing with the documenter role's file-write boundary.

#1557 TASK-1-10: hosts the ``apply_epic`` agent's refine-mode and
plan-mode prompts.
"""

from .apply_epic import APPLY_EPIC_PLAN_PROMPT, APPLY_EPIC_REFINE_PROMPT

__all__ = ["APPLY_EPIC_PLAN_PROMPT", "APPLY_EPIC_REFINE_PROMPT"]
