"""HITL checkpoint handler for the egg-sdlc CLI.

When a pipeline reaches an awaiting_human state, this module presents
the draft document and offers interactive options for resolution.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
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


def _get_contract_key(
    pipeline_mode: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str | None:
    """Return the contract file key for the current pipeline.

    Issue mode uses the issue number as key; local mode uses the pipeline ID.
    Returns None if the required identifier is missing.
    """
    if pipeline_mode == "local":
        return pipeline_id if pipeline_id else None
    # issue mode (default)
    return str(issue_number) if issue_number else None


def _load_pending_contract_decisions(
    repo_path: Path,
    contract_key: str,
) -> list[dict[str, Any]]:
    """Load pending HITL decisions from a contract JSON file.

    Returns decisions where type=="hitl" and resolved==False.
    Returns [] on missing file, parse error, or unexpected structure.
    """
    contract_path = repo_path / ".egg-state" / "contracts" / f"{contract_key}.json"
    if not contract_path.exists():
        return []
    try:
        data = json.loads(contract_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    decisions = data.get("decisions", [])
    if not isinstance(decisions, list):
        return []
    return [
        d
        for d in decisions
        if isinstance(d, dict) and d.get("type") == "hitl" and d.get("resolved") is False
    ]


def _resolve_contract_decision(
    repo_path: Path,
    contract_key: str,
    decision_id: str,
    resolution: str,
) -> bool:
    """Resolve a contract decision by ID via atomic JSON rewrite.

    Sets resolved=True, resolution, resolved_by="human", resolved_at=<now>.
    Returns True on success, False if the decision was not found or on error.

    Note: This uses a read-modify-write cycle without file locking (TOCTOU).
    If an agent mutates the contract file (via the gateway) between our read
    and write, the agent's changes will be lost. This is acceptable for now
    because the CLI is the only writer during human review, and the gateway's
    ``/api/v1/contract/mutate`` endpoint could be used instead for full
    concurrency safety in the future.
    """
    contract_path = repo_path / ".egg-state" / "contracts" / f"{contract_key}.json"
    if not contract_path.exists():
        return False
    try:
        data = json.loads(contract_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    decisions = data.get("decisions", [])
    found = False
    for d in decisions:
        if isinstance(d, dict) and d.get("id") == decision_id:
            d["resolved"] = True
            d["resolution"] = resolution
            d["resolved_by"] = "human"
            d["resolved_at"] = datetime.now(UTC).isoformat()
            found = True
            break

    if not found:
        return False

    # Atomic write: write to temp file then rename
    try:
        contract_dir = contract_path.parent
        fd, tmp_path = tempfile.mkstemp(dir=str(contract_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, str(contract_path))
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError:
        return False

    return True


def _handle_contract_questions(
    repo_path: Path,
    contract_key: str,
    pending: list[dict[str, Any]],
) -> str:
    """Interactively handle pending contract decisions.

    For each decision: options → numbered choice, no options → free-text.
    User can skip (s) individual questions or return to menu (q).
    Returns "answered" when done (some or all answered), "quit" to return to menu.
    """
    print(f"\n{BOLD}  Open Questions ({len(pending)}){RESET}")

    for idx, decision in enumerate(pending):
        d_id = decision.get("id", "unknown")
        question = decision.get("question", "No question text")
        options = decision.get("options", [])

        print(f"\n  {BOLD}[{idx + 1}/{len(pending)}] {question}{RESET}")

        if options:
            # Numbered choice
            for i, opt in enumerate(options, 1):
                label = opt.get("label", opt) if isinstance(opt, dict) else str(opt)
                print(f"    {CYAN}[{i}]{RESET} {label}")
            print(f"    {DIM}[s] Skip  [q] Return to menu{RESET}")

            valid_nums = {str(i) for i in range(1, len(options) + 1)}
            valid = valid_nums | {"s", "q"}
            choice = _prompt_choice(f"    {BOLD}Choose:{RESET} ", valid)

            if choice in ("q", "c"):
                return "quit"
            if choice == "s":
                continue

            selected_opt = options[int(choice) - 1]
            label = (
                selected_opt.get("label", selected_opt)
                if isinstance(selected_opt, dict)
                else str(selected_opt)
            )
            if _resolve_contract_decision(repo_path, contract_key, d_id, label):
                _print_confirmation(f"Answered: {label}")
            else:
                print(f"    {RED}Failed to save answer.{RESET}")
        else:
            # Free-text input — use /s and /q prefixes to avoid intercepting
            # legitimate single-letter answers like "q" or "s".
            print(f"    {DIM}/s Skip  /q Return to menu{RESET}")
            try:
                answer = input(f"    {BOLD}Answer:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return "quit"

            if answer.lower() == "/q":
                return "quit"
            if answer.lower() == "/s" or not answer:
                continue

            if _resolve_contract_decision(repo_path, contract_key, d_id, answer):
                _print_confirmation("Answer saved")
            else:
                print(f"    {RED}Failed to save answer.{RESET}")

    return "answered"


def _display_draft_preview(content: str, max_lines: int = 40) -> None:
    """Display a preview of the draft document."""
    lines = content.split("\n")
    print(f"\n{BOLD}--- Draft Preview ---{RESET}")
    for i, line in enumerate(lines[:max_lines]):
        print(f"  {DIM}{i + 1:3d}{RESET}  {line}")
    if len(lines) > max_lines:
        print(f"  {DIM}... ({len(lines) - max_lines} more lines){RESET}")
    print(f"{BOLD}--- End Preview ---{RESET}\n")


def _run_pager(cmd: list[str], tmp_path: str) -> bool:
    """Run a pager command on tmp_path. Returns True on success."""
    try:
        result = subprocess.run(
            [*cmd, tmp_path],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _display_in_pager(content: str) -> None:
    """Display markdown content with the best available pager.

    Strategy:
    1. If $PAGER is set, use it verbatim (respect user preference).
    2. Else if ``glow`` is on PATH, try ``glow -p`` (rendered markdown).
    3. Else fall back to ``less -R``.

    All paths fall back to ``print(content)`` on total failure.
    """
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            explicit_pager = os.environ.get("PAGER")
            if explicit_pager:
                if _run_pager(shlex.split(explicit_pager), tmp_path):
                    return
                print(content)
                return

            if shutil.which("glow"):
                if _run_pager(["glow", "-p"], tmp_path):
                    return
                # glow failed — try less -R before giving up
                if _run_pager(["less", "-R"], tmp_path):
                    return
                print(content)
                return

            if _run_pager(["less", "-R"], tmp_path):
                return
            print(content)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except OSError:
        print(content)


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
    from llm.runner import build_claude_cmd

    try:
        cmd = build_claude_cmd()
    except FileNotFoundError:
        print(f"{RED}Claude CLI not found. Is it installed?{RESET}")
        return

    # Load HITL editing rules
    rules_path = Path(__file__).parent / "data" / "hitl_editing_rules.md"
    rules_text = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""

    # Build context-specific prompt
    prompt_parts: list[str] = []
    if rules_text:
        prompt_parts.append(rules_text)
    context_parts: list[str] = []
    if phase:
        context_parts.append(f"Current phase: {phase}.")
    if issue_number is not None:
        context_parts.append(f"Issue: #{issue_number}.")
    if draft_rel:
        context_parts.append(
            f"Draft file: {draft_rel}. "
            f"Start by reading `{draft_rel}` and showing its content to the user."
        )
    if context_parts:
        prompt_parts.append(" ".join(context_parts))

    if prompt_parts:
        cmd.extend(["--append-system-prompt", "\n\n".join(prompt_parts)])

    try:
        subprocess.run(
            cmd,
            cwd=str(repo_path),
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except Exception as e:
        print(f"{RED}Failed to launch Claude: {e}{RESET}")


def _prompt_choice(prompt: str, valid: set[str]) -> str:
    """Prompt the user for a choice, retrying on invalid input."""
    while True:
        try:
            choice = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "c"  # Cancel on EOF/interrupt
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


def _resolve_with_json(
    client: OrchClient,
    pipeline_id: str,
    decision_id: str,
    payload: dict[str, Any],
) -> bool:
    """Resolve a decision with a JSON payload. Returns True on success."""
    try:
        client.resolve_decision(pipeline_id, decision_id, json.dumps(payload))
        return True
    except OrchestratorError as e:
        print(f"\n  {RED}Failed to resolve decision: {e}{RESET}")
        return False


def _print_confirmation(message: str) -> None:
    """Print a confirmation message with a checkmark."""
    print(f"\n  {GREEN}✓ {message}{RESET}")


def _display_universal_options() -> None:
    """Display universal options available on every decision type."""
    print(f"\n  {DIM}--- Universal ---{RESET}")
    print(f"  {CYAN}[f]{RESET} General feedback")
    print(f"  {CYAN}[a]{RESET} Change approach / suggest different approach")
    print(f"  {CYAN}[c]{RESET} Cancel pipeline")


def _handle_universal_option(
    choice: str,
    client: OrchClient,
    pipeline_id: str,
    decision_id: str,
) -> str | None:
    """Handle a universal option. Returns 'resolved'/'cancelled' or None if not a universal option."""
    if choice == "f":
        feedback = _prompt_text(f"\n  {BOLD}Enter general feedback (empty line to finish):{RESET}")
        if not feedback.strip():
            print(f"  {DIM}No feedback entered.{RESET}")
            return None
        # General feedback is attached to an approve action
        if _resolve_with_json(
            client,
            pipeline_id,
            decision_id,
            {
                "action": "approve",
                "feedback": feedback,
            },
        ):
            _print_confirmation("Approved with feedback")
            return "resolved"
        return None

    elif choice == "a":
        feedback = _prompt_text(
            f"\n  {BOLD}Describe the approach change you'd like (empty line to finish):{RESET}"
        )
        if not feedback.strip():
            print(f"  {DIM}No feedback entered.{RESET}")
            return None
        if _resolve_with_json(
            client,
            pipeline_id,
            decision_id,
            {
                "action": "change_approach",
                "feedback": feedback,
            },
        ):
            _print_confirmation("Change approach requested")
            return "resolved"
        return None

    elif choice == "c":
        try:
            client.cancel_pipeline(pipeline_id)
            _print_confirmation("Pipeline cancelled")
            return "cancelled"
        except OrchestratorError as e:
            print(f"\n  {RED}Failed to cancel pipeline: {e}{RESET}")
            return "cancelled"

    return None


def _handle_phase_gate(
    client: OrchClient,
    pipeline_id: str,
    decision: dict[str, Any],
    repo_path: Path,
    draft_rel: str | None,
    draft_content: str | None,
    phase: str,
    issue_number: int | None,
    *,
    pipeline_mode: str = "issue",
) -> str:
    """Handle a phase_gate decision with draft review options."""
    decision_id = decision.get("id", "unknown")
    draft_path = repo_path / draft_rel if draft_rel else None
    phase_label = "analysis" if phase == "refine" else phase

    while True:
        # Load pending contract decisions each iteration (may change after answering).
        # pipeline_id doubles as contract key in local mode (e.g., "local-a1b2c3d4").
        contract_key = _get_contract_key(pipeline_mode, issue_number, pipeline_id)
        pending_contract = (
            _load_pending_contract_decisions(repo_path, contract_key) if contract_key else []
        )

        print(f"\n  {BOLD}Options:{RESET}")
        if draft_content:
            print(f"  {CYAN}[v]{RESET} View full document")
        print(f"  {CYAN}[1]{RESET} Edit with $EDITOR ({os.environ.get('EDITOR', 'vim')})")
        print(f"  {CYAN}[2]{RESET} Start Claude for AI-assisted editing")
        print(f"  {CYAN}[3]{RESET} Approve and advance to next phase")
        print(f"  {CYAN}[4]{RESET} Request changes")
        _display_universal_options()
        if pending_contract:
            n = len(pending_contract)
            print(f"\n  {YELLOW}[q]{RESET} Answer open questions ({n} pending)")

        valid = {"1", "2", "3", "4", "f", "a", "c"}
        if draft_content:
            valid.add("v")
        if pending_contract:
            valid.add("q")
        v_hint = "/v" if draft_content else ""
        q_hint = "/q" if pending_contract else ""
        choice = _prompt_choice(f"\n  {BOLD}Choose [1-4{v_hint}/f/a/c{q_hint}]:{RESET} ", valid)

        # Check universal options first
        result = _handle_universal_option(choice, client, pipeline_id, decision_id)
        if result:
            return result

        if choice == "v" and draft_content:
            _display_in_pager(draft_content)
            continue

        if choice == "1":
            if draft_path:
                if not draft_path.exists():
                    draft_path.parent.mkdir(parents=True, exist_ok=True)
                    draft_path.write_text(
                        draft_content if draft_content else f"# Draft: {phase}\n\n"
                    )
                print(f"\n  Opening {draft_path.name} in editor...")
                if _launch_editor(draft_path):
                    print(f"  {GREEN}File saved. You can now approve or continue editing.{RESET}")
                    draft_content = _read_draft(repo_path, draft_rel)
                else:
                    print(f"  {RED}Editor exited with error.{RESET}")
            else:
                print(f"  {RED}No draft file available to edit.{RESET}")

        elif choice == "2":
            print("\n  Launching Claude Code... (type /exit to return)")
            _launch_claude(repo_path, draft_rel, phase, issue_number)
            print(
                f"\n  {GREEN}Returned from Claude. You can now approve or continue editing.{RESET}"
            )
            draft_content = _read_draft(repo_path, draft_rel)

        elif choice == "q" and contract_key:
            _handle_contract_questions(repo_path, contract_key, pending_contract)
            continue

        elif choice == "3":
            # Warn if there are still unanswered contract questions
            if pending_contract:
                n = len(pending_contract)
                print(f"\n  {YELLOW}Warning: {n} question(s) still unanswered.{RESET}")
                confirm = _prompt_choice(f"  {BOLD}Approve anyway? [y/n]:{RESET} ", {"y", "n"})
                if confirm != "y":
                    continue
            if _resolve_with_json(client, pipeline_id, decision_id, {"action": "approve"}):
                _print_confirmation(f"Approved: advancing from {phase_label}")
                return "resolved"

        elif choice == "4":
            feedback = _prompt_text(
                f"\n  {BOLD}Describe changes needed (empty line to finish):{RESET}"
            )
            if not feedback.strip():
                print(f"  {DIM}No feedback entered.{RESET}")
                continue
            if _resolve_with_json(
                client,
                pipeline_id,
                decision_id,
                {
                    "action": "request_changes",
                    "feedback": feedback,
                },
            ):
                _print_confirmation(f"Changes requested for {phase_label}")
                return "resolved"


def _handle_choice(
    client: OrchClient,
    pipeline_id: str,
    decision: dict[str, Any],
) -> str:
    """Handle a choice decision with numbered options."""
    decision_id = decision.get("id", "unknown")
    options = decision.get("options", [])

    while True:
        print(f"\n  {BOLD}Options:{RESET}")
        for i, opt in enumerate(options, 1):
            print(f"  {CYAN}[{i}]{RESET} {opt}")
        _display_universal_options()

        valid_nums = {str(i) for i in range(1, len(options) + 1)}
        valid = valid_nums | {"f", "a", "c"}
        choice = _prompt_choice(f"\n  {BOLD}Choose [1-{len(options)}/f/a/c]:{RESET} ", valid)

        # Check universal options first
        result = _handle_universal_option(choice, client, pipeline_id, decision_id)
        if result:
            return result

        if choice in valid_nums:
            selected = options[int(choice) - 1]
            if _resolve_with_json(
                client,
                pipeline_id,
                decision_id,
                {
                    "action": "select",
                    "selected": selected,
                },
            ):
                _print_confirmation(f"Selected: {selected}")
                return "resolved"


def _handle_feedback(
    client: OrchClient,
    pipeline_id: str,
    decision: dict[str, Any],
) -> str:
    """Handle a feedback decision with structured question prompts."""
    decision_id = decision.get("id", "unknown")
    questions = decision.get("questions", [])

    # If no structured questions, fall back to single free-text input
    if not questions:
        while True:
            _display_universal_options()
            feedback = _prompt_text(f"\n  {BOLD}Enter your response (empty line to finish):{RESET}")
            if not feedback.strip():
                print(f"  {DIM}No response entered.{RESET}")
                # Show universal options for empty input
                valid = {"f", "a", "c", "r"}
                print(f"  {CYAN}[r]{RESET} Retry input")
                choice = _prompt_choice(f"\n  {BOLD}Choose [r/f/a/c]:{RESET} ", valid)
                if choice == "r":
                    continue
                result = _handle_universal_option(choice, client, pipeline_id, decision_id)
                if result:
                    return result
                continue
            if _resolve_with_json(
                client,
                pipeline_id,
                decision_id,
                {
                    "action": "submit_feedback",
                    "answers": {"response": feedback},
                },
            ):
                _print_confirmation("Feedback submitted")
                return "resolved"
            continue

    # Collect answers for each question
    answers: dict[str, str] = {}

    def _collect_answers() -> bool:
        """Collect answers for all questions. Returns False if cancelled."""
        for i, q in enumerate(questions):
            q_id = q.get("id", f"q-{i + 1}")
            q_text = q.get("question", "Question")
            if q_id in answers:
                # Already answered (e.g. during resume after interrupt)
                continue
            print(f"\n  {BOLD}Q: {q_text}{RESET}")
            try:
                answer = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            answers[q_id] = answer
        return True

    while True:
        answers.clear()
        if not _collect_answers():
            # Interrupted mid-collection — show partial answers with recovery
            print(
                f"\n  {YELLOW}Input interrupted. {len(answers)}/{len(questions)} answers collected.{RESET}"
            )
            if answers:
                print(f"\n  {BOLD}Partial answers:{RESET}")
                for i, q in enumerate(questions):
                    q_id = q.get("id", f"q-{i + 1}")
                    q_text = q.get("question", "Question")
                    if q_id in answers:
                        print(f"  {DIM}{i + 1}.{RESET} {q_text}: {answers[q_id]}")
                    else:
                        print(f"  {DIM}{i + 1}.{RESET} {q_text}: {RED}(unanswered){RESET}")
            valid = {"s", "r", "c"}
            print(f"\n  {CYAN}[s]{RESET} Submit partial answers")
            print(f"  {CYAN}[r]{RESET} Resume from last question")
            print(f"  {CYAN}[c]{RESET} Discard all")
            choice = _prompt_choice(f"\n  {BOLD}Choose [s/r/c]:{RESET} ", valid)
            if choice == "s" and answers:
                break  # Submit what we have
            elif choice == "r":
                # Resume: keep existing answers, re-enter _collect_answers
                if _collect_answers():
                    break  # All collected, proceed to review
                continue  # Interrupted again
            else:
                # Discard — return to caller to re-enter
                return (
                    _handle_universal_option("c", client, pipeline_id, decision_id) or "cancelled"
                )
        else:
            break

    # Review-and-submit loop
    while True:
        print(f"\n  {BOLD}Review your answers:{RESET}")
        for i, q in enumerate(questions):
            q_id = q.get("id", f"q-{i + 1}")
            q_text = q.get("question", "Question")
            answer = answers.get(q_id, "(unanswered)")
            print(f"  {DIM}{i + 1}.{RESET} {q_text}")
            print(f"     {answer}")

        print(f"\n  {CYAN}[s]{RESET} Submit answers")
        print(f"  {CYAN}[r]{RESET} Redo a question")
        _display_universal_options()

        valid = {"s", "r", "f", "a", "c"}
        choice = _prompt_choice(f"\n  {BOLD}Choose [s/r/f/a/c]:{RESET} ", valid)

        # Check universal options
        result = _handle_universal_option(choice, client, pipeline_id, decision_id)
        if result:
            return result

        if choice == "s":
            if _resolve_with_json(
                client,
                pipeline_id,
                decision_id,
                {
                    "action": "submit_feedback",
                    "answers": answers,
                },
            ):
                _print_confirmation(f"Feedback submitted ({len(answers)} answer(s))")
                return "resolved"

        elif choice == "r":
            try:
                num_str = input(f"  Which question? (1-{len(questions)}): ").strip()
                num = int(num_str)
                if 1 <= num <= len(questions):
                    q = questions[num - 1]
                    q_id = q.get("id", f"q-{num}")
                    q_text = q.get("question", "Question")
                    print(f"\n  {BOLD}Q: {q_text}{RESET}")
                    try:
                        answer = input("  > ").strip()
                        answers[q_id] = answer
                    except (EOFError, KeyboardInterrupt):
                        print()
                else:
                    print(f"  {RED}Invalid question number.{RESET}")
            except (ValueError, EOFError, KeyboardInterrupt):
                print(f"  {RED}Invalid input.{RESET}")


def _handle_generic(
    client: OrchClient,
    pipeline_id: str,
    decision: dict[str, Any],
    repo_path: Path,
    draft_rel: str | None,
    draft_content: str | None,
    phase: str,
    issue_number: int | None,
) -> str:
    """Fallback generic menu for unknown decision types (backward compatibility)."""
    decision_id = decision.get("id", "unknown")
    draft_path = repo_path / draft_rel if draft_rel else None

    while True:
        print(f"\n  {BOLD}Options:{RESET}")
        print(f"  {CYAN}[1]{RESET} Edit with $EDITOR ({os.environ.get('EDITOR', 'vim')})")
        print(f"  {CYAN}[2]{RESET} Start Claude for AI-assisted editing")
        print(f"  {CYAN}[3]{RESET} Approve and advance to next phase")
        print(f"  {CYAN}[4]{RESET} Provide feedback (text input)")
        print(f"  {CYAN}[5]{RESET} Cancel pipeline")

        choice = _prompt_choice(f"\n  {BOLD}Choose [1-5]:{RESET} ", {"1", "2", "3", "4", "5", "c"})

        if choice == "1":
            if draft_path:
                if not draft_path.exists():
                    draft_path.parent.mkdir(parents=True, exist_ok=True)
                    draft_path.write_text(
                        draft_content if draft_content else f"# Draft: {phase}\n\n"
                    )
                print(f"\n  Opening {draft_path.name} in editor...")
                if _launch_editor(draft_path):
                    print(f"  {GREEN}File saved. You can now approve or continue editing.{RESET}")
                    draft_content = _read_draft(repo_path, draft_rel)
                else:
                    print(f"  {RED}Editor exited with error.{RESET}")
            else:
                print(f"  {RED}No draft file available to edit.{RESET}")
            continue

        elif choice == "2":
            print("\n  Launching Claude Code... (type /exit to return)")
            _launch_claude(repo_path, draft_rel, phase, issue_number)
            print(
                f"\n  {GREEN}Returned from Claude. You can now approve or continue editing.{RESET}"
            )
            draft_content = _read_draft(repo_path, draft_rel)
            continue

        elif choice == "3":
            try:
                client.resolve_decision(pipeline_id, decision_id, "Approved")
                print(f"\n  {GREEN}Decision resolved: Approved{RESET}")
                return "resolved"
            except OrchestratorError as e:
                print(f"\n  {RED}Failed to resolve decision: {e}{RESET}")
                continue

        elif choice == "4":
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

        elif choice in ("5", "c"):
            try:
                client.cancel_pipeline(pipeline_id)
                print(f"\n  {YELLOW}Pipeline cancelled.{RESET}")
                return "cancelled"
            except OrchestratorError as e:
                print(f"\n  {RED}Failed to cancel pipeline: {e}{RESET}")
                return "cancelled"


def handle_hitl_checkpoint(
    client: OrchClient,
    pipeline_id: str,
    decision: dict[str, Any],
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
) -> str:
    """Handle a HITL checkpoint interactively.

    Dispatches to type-specific handlers based on decision_type:
    - phase_gate: Draft review with approve/request changes
    - choice: Numbered option selection
    - feedback: Structured question prompts
    - (unknown): Falls back to generic menu

    Returns:
        "resolved" if the decision was resolved (pipeline should continue),
        "cancelled" if the pipeline was cancelled.
    """
    question = decision.get("question", "Decision required")
    context = decision.get("context", "")
    decision_type = decision.get("decision_type", "choice")

    # Use explicit phase from decision if available, fall back to regex detection
    raw_phase = decision.get("phase")
    phase = raw_phase if raw_phase is not None else _detect_phase(question, context)

    # If phase is still unknown, try fetching from the pipeline's current state
    if phase == "unknown":
        try:
            pipeline_info = client.get_pipeline(pipeline_id)
            pipeline_phase = pipeline_info.get("pipeline", pipeline_info).get("current_phase")
            if pipeline_phase:
                phase = pipeline_phase
        except Exception:
            pass

    # Find and read the draft
    repo_path = _find_repo_path()
    draft_rel = _get_draft_path(phase, pipeline_mode, issue_number, pipeline_id)
    draft_content = _read_draft(repo_path, draft_rel)

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

    # Show full document in pager for phase gates
    if decision_type == "phase_gate":
        if draft_content:
            _display_in_pager(draft_content)
        elif draft_rel:
            print(f"\n  {DIM}Draft file: {draft_rel} (not found){RESET}")

    # Dispatch to type-specific handler
    if decision_type == "phase_gate":
        return _handle_phase_gate(
            client,
            pipeline_id,
            decision,
            repo_path,
            draft_rel,
            draft_content,
            phase,
            issue_number,
            pipeline_mode=pipeline_mode,
        )
    elif decision_type == "choice":
        options = decision.get("options", [])
        if options:
            return _handle_choice(client, pipeline_id, decision)
        # No options — fall through to generic
    elif decision_type == "feedback":
        return _handle_feedback(client, pipeline_id, decision)

    # Unknown type or choice without options — generic fallback
    if draft_content and decision_type != "phase_gate":
        _display_draft_preview(draft_content)
    elif draft_rel and decision_type != "phase_gate":
        print(f"\n  {DIM}Draft file: {draft_rel} (not found){RESET}")

    return _handle_generic(
        client,
        pipeline_id,
        decision,
        repo_path,
        draft_rel,
        draft_content,
        phase,
        issue_number,
    )


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
