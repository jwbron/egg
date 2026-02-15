#!/usr/bin/env python3
"""
CLI entry points for prompt builder functions.

These provide Python equivalents of the action/build-*-prompt.sh scripts.
Each function reads from the same environment variables as the shell scripts
and writes the same output format ($GITHUB_OUTPUT, prompt file).

Usage:
    python -m egg_prompts.cli review          # equivalent to build-review-prompt.sh
    python -m egg_prompts.cli autofixer       # equivalent to build-autofixer-prompt.sh
    python -m egg_prompts.cli conflict        # equivalent to build-conflict-prompt.sh
    python -m egg_prompts.cli feedback        # equivalent to build-feedback-prompt.sh
    python -m egg_prompts.cli contract        # equivalent to build-contract-verification-prompt.sh
    python -m egg_prompts.cli agent-design    # equivalent to build-agent-mode-design-review-prompt.sh
    python -m egg_prompts.cli doc-updater     # equivalent to build-doc-updater-prompt.sh
"""

import os
import sys
from pathlib import Path

from egg_prompts.builders import (
    build_agent_design_review_prompt,
    build_autofixer_prompt,
    build_conflict_prompt,
    build_contract_verification_prompt,
    build_doc_updater_prompt,
    build_feedback_prompt,
    build_review_prompt,
)


def _write_output(prompt: str, model: str, prompt_file: Path) -> None:
    """Write prompt to file and set GITHUB_OUTPUT variables."""
    prompt_file.write_text(prompt)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"prompt_file={prompt_file}\n")
            f.write(f"model={model}\n")

    print(f"Prompt built: {len(prompt)} chars, model={model}")


def cmd_review() -> None:
    """Build review prompt (equivalent to build-review-prompt.sh)."""
    pr_number = os.environ["PR_NUMBER"]
    github_repo = os.environ["GITHUB_REPOSITORY"]
    last_review_commit = os.environ.get("LAST_REVIEW_COMMIT", "")
    runner_temp = os.environ.get("RUNNER_TEMP", "/tmp")

    prompt, model = build_review_prompt(
        pr_number=pr_number,
        github_repository=github_repo,
        last_review_commit=last_review_commit,
    )

    prompt_file = Path(runner_temp) / f"review-prompt-{pr_number}.txt"
    _write_output(prompt, model, prompt_file)

    review_type = "re-review" if last_review_commit else "initial"
    print(f"Review type: {review_type}")


def cmd_autofixer() -> None:
    """Build autofixer prompt (equivalent to build-autofixer-prompt.sh)."""
    pr_number = os.environ["PR_NUMBER"]
    github_repo = os.environ["GITHUB_REPOSITORY"]
    runner_temp = os.environ.get("RUNNER_TEMP", "/tmp")

    prompt, model = build_autofixer_prompt(
        pr_number=pr_number,
        github_repository=github_repo,
    )

    prompt_file = Path(runner_temp) / f"autofixer-prompt-{pr_number}.txt"
    _write_output(prompt, model, prompt_file)


def cmd_conflict() -> None:
    """Build conflict prompt (equivalent to build-conflict-prompt.sh)."""
    pr_number = os.environ["PR_NUMBER"]
    github_repo = os.environ["GITHUB_REPOSITORY"]
    base_ref = os.environ.get("BASE_REF", "main")
    runner_temp = os.environ.get("RUNNER_TEMP", "/tmp")

    prompt, model = build_conflict_prompt(
        pr_number=pr_number,
        github_repository=github_repo,
        base_ref=base_ref,
    )

    prompt_file = Path(runner_temp) / f"conflict-prompt-{pr_number}.txt"
    _write_output(prompt, model, prompt_file)


def cmd_feedback() -> None:
    """Build feedback prompt (equivalent to build-feedback-prompt.sh)."""
    pr_number = os.environ["PR_NUMBER"]
    github_repo = os.environ["GITHUB_REPOSITORY"]
    runner_temp = os.environ.get("RUNNER_TEMP", "/tmp")

    prompt, model = build_feedback_prompt(
        pr_number=pr_number,
        github_repository=github_repo,
    )

    prompt_file = Path(runner_temp) / f"feedback-prompt-{pr_number}.txt"
    _write_output(prompt, model, prompt_file)


def cmd_contract() -> None:
    """Build contract verification prompt."""
    pr_number = os.environ["PR_NUMBER"]
    github_repo = os.environ["GITHUB_REPOSITORY"]
    runner_temp = os.environ.get("RUNNER_TEMP", "/tmp")

    prompt, model = build_contract_verification_prompt(
        pr_number=pr_number,
        github_repository=github_repo,
    )

    prompt_file = Path(runner_temp) / f"contract-verification-prompt-{pr_number}.txt"
    _write_output(prompt, model, prompt_file)


def cmd_agent_design() -> None:
    """Build agent-mode design review prompt."""
    pr_number = os.environ["PR_NUMBER"]
    github_repo = os.environ["GITHUB_REPOSITORY"]
    last_review_commit = os.environ.get("LAST_REVIEW_COMMIT", "")
    runner_temp = os.environ.get("RUNNER_TEMP", "/tmp")

    prompt, model = build_agent_design_review_prompt(
        pr_number=pr_number,
        github_repository=github_repo,
        last_review_commit=last_review_commit,
    )

    prompt_file = Path(runner_temp) / f"agent-design-review-prompt-{pr_number}.txt"
    _write_output(prompt, model, prompt_file)


def cmd_doc_updater() -> None:
    """Build doc updater prompt (equivalent to build-doc-updater-prompt.sh).

    Note: This is a simplified version. The shell script runs git commands
    to compute changed_files, commit_messages, etc. In the Python version,
    these must be provided as environment variables or computed separately.
    """
    github_repo = os.environ["GITHUB_REPOSITORY"]
    runner_temp = os.environ.get("RUNNER_TEMP", "/tmp")
    commit_sha = os.environ.get("COMMIT_SHA", "HEAD~1")
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    # These would normally be computed by git commands
    # When called from the shell wrapper, they're pre-computed
    changed_files = os.environ.get("CHANGED_FILES", "")
    commit_messages = os.environ.get("COMMIT_MESSAGES", "")
    diff_stats = os.environ.get("DIFF_STATS", "")
    new_files = os.environ.get("NEW_FILES", "")
    related_docs = os.environ.get("RELATED_DOCS", "")
    high_risk_flags = os.environ.get("HIGH_RISK_FLAGS", "")
    high_risk_instructions = os.environ.get("HIGH_RISK_INSTRUCTIONS", "")

    if not changed_files:
        print("No code files changed, skipping doc-updater")
        prompt_file = Path(runner_temp) / "doc-updater-prompt.txt"
        prompt_file.write_text(f"No code files changed since {commit_sha}. Nothing to do.")

        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"prompt_file={prompt_file}\n")
                f.write("model=haiku\n")
        return

    prompt, model = build_doc_updater_prompt(
        github_repository=github_repo,
        changed_files=changed_files,
        commit_messages=commit_messages,
        diff_stats=diff_stats,
        new_files=new_files,
        related_docs=related_docs,
        high_risk_flags=high_risk_flags,
        high_risk_instructions=high_risk_instructions,
        commit_sha=commit_sha,
        dry_run=dry_run,
    )

    prompt_file = Path(runner_temp) / "doc-updater-prompt.txt"
    _write_output(prompt, model, prompt_file)


COMMANDS = {
    "review": cmd_review,
    "autofixer": cmd_autofixer,
    "conflict": cmd_conflict,
    "feedback": cmd_feedback,
    "contract": cmd_contract,
    "agent-design": cmd_agent_design,
    "doc-updater": cmd_doc_updater,
}


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: python -m egg_prompts.cli <command>")
        print(f"Commands: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
