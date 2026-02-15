"""
Shared prompt builder module for egg agents.

Provides reusable functions for building prompts used in both the GHA action
scripts and the orchestrator's pipeline prompt builders. Implements the 2-tier
lookup pattern for conventions and rules files:

1. Repo-specific: .egg/{name}-conventions.md or .egg/{name}-rules.md
2. Default: action/{name}-conventions.md (bundled with egg)

Usage:
    from egg_prompts import load_conventions, load_rules
    from egg_prompts.builders import build_review_prompt, build_autofixer_prompt
"""

from egg_prompts.conventions import load_conventions, load_rules

__all__ = ["load_conventions", "load_rules"]
