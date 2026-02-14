"""HITL checkpoint handler for the egg-sdlc CLI.

When a pipeline reaches an awaiting_human state, this module presents
the draft document and offers interactive options for resolution.
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .orch_client import OrchClient, OrchestratorError

# ANSI escape codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def _get_draft_path(
    phase: str,
    pipeline_mode: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str | None:
    """Return relative path to the draft file for a phase.

    Mirrors the logic in orchestrator/routes/pipelines.py:_get_draft_path.
    """
    is_local = pipeline_mode == "local"
    if is_local:
        prefix = pipeline_id if pipeline_id else "local"
    else:
        prefix = str(issue_number) if issue_number else "unknown"

    if phase == "refine":
        return f".egg-state/drafts/{prefix}-analysis.md"
    elif phase == "implement":
        return None
    else:
        return f".egg-state/drafts/{prefix}-{phase}.md"


def _parse_egg_repos() -> list[str]:
    """Parse the EGG_REPOS env var into a list of owner/repo strings."""
    egg_repos = os.environ.get("EGG_REPOS", "").strip()
    if not egg_repos:
        return []
    return [r.strip() for r in egg_repos.split(",") if r.strip()]


def _find_repo_path() -> Path:
    """Find the repository root path.

    Tries multiple strategies since .git is shadowed by tmpfs in
    gateway-managed containers:
    1. EGG_REPOS env var → derive path from ~/repos/<repo-name>
    2. Single subdirectory under ~/repos/
    3. git rev-parse (works outside containers)
    4. Walk up from cwd looking for .git
    """
    repos_dir = Path.home() / "repos"

    # Strategy 1: EGG_REPOS env var (set by exec_in_new_container)
    repos = _parse_egg_repos()
    if repos:
        if len(repos) == 1:
            # "owner/name" → use "name" as directory
            repo_name = repos[0].split("/")[-1]
            candidate = repos_dir / repo_name
            if candidate.is_dir():
                return candidate

    # Strategy 2: single repo under ~/repos/
    if repos_dir.is_dir():
        subdirs = [d for d in repos_dir.iterdir() if d.is_dir()]
        if len(subdirs) == 1:
            return subdirs[0]

    # Strategy 3: git rev-parse (works when .git is real)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    # Strategy 4: walk up from cwd looking for .git
    cwd = Path.cwd()
    while cwd != cwd.parent:
        if (cwd / ".git").exists():
            return cwd
        cwd = cwd.parent
    return Path.cwd()


def _read_draft(repo_path: Path, draft_rel: str | None) -> str | None:
    """Read a draft file, returning its content or None."""
    if not draft_rel:
        return None
    draft_path = repo_path / draft_rel
    if not draft_path.exists():
        return None
    try:
        return draft_path.read_text()
    except Exception:
        return None


def _display_draft_preview(content: str, max_lines: int = 40) -> None:
    """Display a preview of the draft document."""
    lines = content.split("\n")
    print(f"\n{BOLD}--- Draft Preview ---{RESET}")
    for i, line in enumerate(lines[:max_lines]):
        print(f"  {DIM}{i + 1:3d}{RESET}  {line}")
    if len(lines) > max_lines:
        print(f"  {DIM}... ({len(lines) - max_lines} more lines){RESET}")
    print(f"{BOLD}--- End Preview ---{RESET}\n")


def _launch_editor(file_path: Path) -> bool:
    """Launch the user's preferred editor on the file.

    Returns True if editor exited successfully.
    """
    editor = os.environ.get("EDITOR", "vim")
    try:
        result = subprocess.run(
            [*shlex.split(editor), str(file_path)],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        return result.returncode == 0
    except FileNotFoundError:
        print(f"{RED}Editor '{editor}' not found. Set $EDITOR to your preferred editor.{RESET}")
        return False
    except Exception as e:
        print(f"{RED}Failed to launch editor: {e}{RESET}")
        return False


def _launch_claude(
    repo_path: Path,
    draft_rel: str | None = None,
    phase: str | None = None,
    issue_number: int | None = None,
) -> None:
    """Launch an interactive Claude Code session with draft context."""
    cmd = ["claude"]

    # Inject context so Claude knows which draft to edit
    if draft_rel:
        parts = ["You are helping review/edit a draft in an SDLC pipeline."]
        if phase:
            parts.append(f"Current phase: {phase}.")
        if issue_number:
            parts.append(f"Issue: #{issue_number}.")
        parts.append(
            f"Draft file: {draft_rel}. "
            f"Start by reading `{draft_rel}` and showing its content to the user."
        )
        cmd.extend(["--append-system-prompt", " ".join(parts)])

    try:
        subprocess.run(
            cmd,
            cwd=str(repo_path),
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except FileNotFoundError:
        print(f"{RED}Claude CLI not found. Is it installed?{RESET}")
    except Exception as e:
        print(f"{RED}Failed to launch Claude: {e}{RESET}")


def _prompt_choice(prompt: str, valid: set[str]) -> str:
    """Prompt the user for a choice, retrying on invalid input."""
    while True:
        try:
            choice = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "5"  # Cancel on EOF/interrupt
        if choice in valid:
            return choice
        print(f"  {RED}Invalid choice. Enter one of: {', '.join(sorted(valid))}{RESET}")


def _prompt_text(prompt: str) -> str:
    """Prompt for multi-line text input. Empty line to finish."""
    print(prompt)
    lines = []
    try:
        while True:
            line = input("  > ")
            if line == "":
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        print()
    return "\n".join(lines)


def handle_hitl_checkpoint(
    client: OrchClient,
    pipeline_id: str,
    decision: dict[str, Any],
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
) -> str:
    """Handle a HITL checkpoint interactively.

    Returns:
        "resolved" if the decision was resolved (pipeline should continue),
        "cancelled" if the pipeline was cancelled.
    """
    decision_id = decision.get("id", "unknown")
    question = decision.get("question", "Decision required")
    context = decision.get("context", "")

    # Determine current phase from the question or context
    phase = _detect_phase(question, context)

    # Find and read the draft
    repo_path = _find_repo_path()
    draft_rel = _get_draft_path(phase, pipeline_mode, issue_number, pipeline_id)
    draft_content = _read_draft(repo_path, draft_rel)
    draft_path = repo_path / draft_rel if draft_rel else None

    # Fall back to decision context if local draft file not found.
    # The draft lives in the agent's worktree which may not be mounted
    # here, but the orchestrator reads it and attaches it as context.
    if not draft_content and context:
        draft_content = context

    # Display decision info
    print(f"\n{BOLD}{YELLOW}{'=' * 60}{RESET}")
    print(f"{BOLD}{YELLOW}  HUMAN DECISION REQUIRED{RESET}")
    print(f"{BOLD}{YELLOW}{'=' * 60}{RESET}")
    print(f"\n  {BOLD}Pipeline:{RESET} {pipeline_id}")
    print(f"  {BOLD}Phase:{RESET}    {phase}")
    print(f"  {BOLD}Question:{RESET} {question}")

    # Show draft preview if available
    if draft_content:
        _display_draft_preview(draft_content)
    elif draft_rel:
        print(f"\n  {DIM}Draft file: {draft_rel} (not found){RESET}")

    # Interactive menu loop
    while True:
        print(f"\n  {BOLD}Options:{RESET}")
        print(f"  {CYAN}[1]{RESET} Edit with $EDITOR ({os.environ.get('EDITOR', 'vim')})")
        print(f"  {CYAN}[2]{RESET} Start Claude for AI-assisted editing")
        print(f"  {CYAN}[3]{RESET} Approve and advance to next phase")
        print(f"  {CYAN}[4]{RESET} Provide feedback (text input)")
        print(f"  {CYAN}[5]{RESET} Cancel pipeline")

        choice = _prompt_choice(f"\n  {BOLD}Choose [1-5]:{RESET} ", {"1", "2", "3", "4", "5"})

        if choice == "1":
            # Edit with $EDITOR
            if draft_path:
                if not draft_path.exists():
                    # Write draft content (from context) so the editor
                    # opens with the actual draft, not a stub.
                    draft_path.parent.mkdir(parents=True, exist_ok=True)
                    draft_path.write_text(
                        draft_content if draft_content else f"# Draft: {phase}\n\n"
                    )
                print(f"\n  Opening {draft_path.name} in editor...")
                if _launch_editor(draft_path):
                    print(f"  {GREEN}File saved. You can now approve or continue editing.{RESET}")
                    # Re-read for preview
                    draft_content = _read_draft(repo_path, draft_rel)
                else:
                    print(f"  {RED}Editor exited with error.{RESET}")
            else:
                print(f"  {RED}No draft file available to edit.{RESET}")
            continue  # Return to menu

        elif choice == "2":
            # Launch Claude
            print("\n  Launching Claude Code... (type /exit to return)")
            _launch_claude(repo_path, draft_rel, phase, issue_number)
            print(
                f"\n  {GREEN}Returned from Claude. You can now approve or continue editing.{RESET}"
            )
            # Re-read draft in case Claude modified it
            draft_content = _read_draft(repo_path, draft_rel)
            continue  # Return to menu

        elif choice == "3":
            # Approve
            try:
                client.resolve_decision(pipeline_id, decision_id, "Approved")
                print(f"\n  {GREEN}Decision resolved: Approved{RESET}")
                return "resolved"
            except OrchestratorError as e:
                print(f"\n  {RED}Failed to resolve decision: {e}{RESET}")
                continue

        elif choice == "4":
            # Feedback
            feedback = _prompt_text(f"\n  {BOLD}Enter feedback (empty line to finish):{RESET}")
            if not feedback.strip():
                print(f"  {DIM}No feedback entered.{RESET}")
                continue
            try:
                client.resolve_decision(pipeline_id, decision_id, feedback)
                print(f"\n  {GREEN}Decision resolved with feedback.{RESET}")
                return "resolved"
            except OrchestratorError as e:
                print(f"\n  {RED}Failed to resolve decision: {e}{RESET}")
                continue

        elif choice == "5":
            # Cancel
            try:
                client.cancel_pipeline(pipeline_id)
                print(f"\n  {YELLOW}Pipeline cancelled.{RESET}")
                return "cancelled"
            except OrchestratorError as e:
                print(f"\n  {RED}Failed to cancel pipeline: {e}{RESET}")
                return "cancelled"


def _detect_phase(question: str, context: str | None = None) -> str:
    """Detect the pipeline phase from the decision payload.

    Uses word-boundary regex on the question text.  The ``context`` argument
    is accepted for forward-compatibility but is not inspected today (the
    orchestrator always passes a string).
    """
    q = question.lower()
    if re.search(r"\brefine\b", q) or re.search(r"\banalysis\b", q):
        return "refine"
    elif re.search(r"\bplan\b", q):
        return "plan"
    elif re.search(r"\bimplement\b", q):
        return "implement"
    elif re.search(r"\bpr\b", q):
        return "pr"
    return "unknown"
