"""
Prompt builder functions for egg agent tasks.

Each function produces a complete prompt string for a specific agent task.
These are Python equivalents of the shell scripts in action/build-*-prompt.sh,
with the same 2-tier lookup pattern for conventions and rules.

Usage:
    from egg_prompts.builders import build_review_prompt

    prompt = build_review_prompt(
        pr_number=123,
        github_repository="owner/repo",
    )
"""

from pathlib import Path

from egg_prompts.conventions import load_conventions, load_rules


# ---------------------------------------------------------------------------
# Review prompt
# ---------------------------------------------------------------------------


def build_review_prompt(
    pr_number: int | str,
    github_repository: str,
    last_review_commit: str = "",
    repo_path: Path | str | None = None,
) -> tuple[str, str]:
    """Build a prompt for PR code review.

    Supports both initial reviews and re-reviews (when last_review_commit
    is provided). Loads review rules and conventions with 2-tier lookup.

    Args:
        pr_number: Pull request number.
        github_repository: owner/repo string.
        last_review_commit: If set, builds a re-review prompt focusing on
            changes since this commit.
        repo_path: Path to target repo for repo-specific overrides.

    Returns:
        Tuple of (prompt_text, model_name).
    """
    is_rereview = bool(last_review_commit)

    # Load rules (2-tier: repo-specific, then inline default)
    review_rules = load_rules("review", repo_path)
    if not review_rules:
        review_rules = _default_review_rules()

    # Load conventions
    conventions = load_conventions("review", repo_path)

    if is_rereview:
        prompt = _build_rereview_prompt(
            pr_number, github_repository, last_review_commit, review_rules, conventions
        )
    else:
        prompt = _build_initial_review_prompt(
            pr_number, github_repository, review_rules, conventions
        )

    return prompt, "opus"


def _default_review_rules() -> str:
    """Return the default inline review rules."""
    return """## Review Rules

### Security (highest priority)
- Injection vulnerabilities (SQL, command, XSS, LDAP, path traversal)
- Authentication/authorization flaws
- Credential exposure, hardcoded secrets
- SSRF, open redirects, unsafe deserialization

### Correctness
- Logic errors, off-by-one, boundary conditions
- Race conditions, deadlocks, concurrency bugs
- Null/undefined handling, missing error paths
- Resource leaks (connections, file handles, memory)

### Robustness
- Missing input validation at trust boundaries
- Unhandled exceptions that could crash the system
- Missing retry logic for transient failures
- Inadequate timeouts for external calls

### Design
- Violations of existing codebase patterns
- Breaking changes to public interfaces
- Tight coupling that will hinder future changes"""


def _build_initial_review_prompt(
    pr_number: int | str,
    github_repository: str,
    review_rules: str,
    conventions: str,
) -> str:
    """Build an initial review prompt."""
    lines = [
        f"Review PR #{pr_number} in {github_repository}.",
        "",
        "## Your Task",
        "",
        "Perform a thorough, comprehensive code review of this pull request.",
        "Report ALL issues you find — a false negative (missing a bug) is far more costly "
        "than comprehensive feedback.",
        "",
        "1. **Read the PR**: `gh pr diff " + str(pr_number) + "`",
        f"2. **Understand context**: `gh pr view {pr_number}` for description and metadata",
        "3. **Research the codebase**: Read relevant files to understand patterns and conventions",
        "4. **Review every changed file** systematically against the rules below",
        "5. **Post your review** using `gh pr review`",
        "",
        review_rules,
        "",
        "## Review Philosophy",
        "",
        "- Be thorough. Check every file, every function, every change.",
        "- Be direct. State issues clearly without softening language.",
        "- Be specific. Reference exact file and line. Explain what and why.",
        "- Suggest fixes. Show correct code when possible.",
        "- Report ALL issues — do not stop after finding a few.",
        "",
    ]

    if conventions:
        lines.append("## Review Conventions\n")
        lines.append(conventions)
        lines.append("")

    lines.append(f"Sign your review with: — Authored by egg")

    return "\n".join(lines)


def _build_rereview_prompt(
    pr_number: int | str,
    github_repository: str,
    last_review_commit: str,
    review_rules: str,
    conventions: str,
) -> str:
    """Build a re-review prompt."""
    lines = [
        f"Re-review PR #{pr_number} in {github_repository}.",
        "",
        "This is a **re-review**. Changes have been made since your last review.",
        "",
        "## Your Task",
        "",
        "1. **Check previous feedback**: `gh pr view " + str(pr_number) + " --comments`",
        "   Read your previous review to understand what issues were raised.",
        "2. **Review the delta**: See what changed since your last review:",
        f"   `git diff {last_review_commit}..HEAD`",
        "3. **Verify issues addressed**: Check that previous review feedback was addressed.",
        "4. **Review new changes thoroughly**: Apply the same review rules to new changes.",
        f"5. **Full PR context** (if needed): `gh pr diff {pr_number}`",
        "6. **Post your review** using `gh pr review`",
        "",
        "Be thorough with the new changes. Report ALL issues found.",
        "",
        review_rules,
        "",
        "## Review Philosophy",
        "",
        "- Be thorough. Check every file, every function, every change.",
        "- Be direct. State issues clearly without softening language.",
        "- Be specific. Reference exact file and line. Explain what and why.",
        "- Report ALL issues — do not stop after finding a few.",
        "",
    ]

    if conventions:
        lines.append("## Review Conventions\n")
        lines.append(conventions)
        lines.append("")

    lines.append(f"Sign your review with: — Authored by egg")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Autofixer prompt
# ---------------------------------------------------------------------------


def build_autofixer_prompt(
    pr_number: int | str,
    github_repository: str,
    repo_path: Path | str | None = None,
) -> tuple[str, str]:
    """Build a prompt for fixing failing checks on a PR.

    Args:
        pr_number: Pull request number.
        github_repository: owner/repo string.
        repo_path: Path to target repo for repo-specific overrides.

    Returns:
        Tuple of (prompt_text, model_name).
    """
    conventions = load_conventions("autofixer", repo_path)

    lines = [
        f"Fix failing checks on PR #{pr_number} in {github_repository}.",
        "",
        "## Your Task",
        "",
        "CI checks are failing on this PR. Investigate and fix all auto-fixable issues "
        "in a single pass.",
        "",
        f"1. **List failing checks**: `gh pr checks {pr_number}`",
        "2. **Get failure logs**: `gh run list --branch <pr-branch> --status failure` "
        "then `gh run view <run-id> --log-failed`",
        "3. **Investigate root causes**: Read the error output and understand each failure",
        "4. **Fix ALL issues** before committing (single-pass workflow)",
        "5. **Verify locally**: Run `make lint`, `make test` — repeat until all pass",
        "6. **Push once**: Commit and push all fixes together",
        "",
        "## Decision Framework",
        "",
        "**Auto-fix when**: The fix is mechanical, there's one obvious solution, "
        "the change is low-risk, and you can verify locally.",
        "",
        "**Report instead when**: Multiple valid approaches exist, the fix requires "
        "business context, the change could break other things, or security implications "
        "need human review.",
        "",
    ]

    if conventions:
        lines.append("## Autofixer Conventions\n")
        lines.append(conventions)
        lines.append("")

    lines.append("Sign all PR comments with: — Authored by egg")

    return "\n".join(lines), "opus"


# ---------------------------------------------------------------------------
# Contract verification prompt
# ---------------------------------------------------------------------------


def build_contract_verification_prompt(
    pr_number: int | str,
    github_repository: str,
    repo_path: Path | str | None = None,
) -> tuple[str, str]:
    """Build a prompt for verifying SDLC contract compliance.

    Args:
        pr_number: Pull request number.
        github_repository: owner/repo string.
        repo_path: Path to target repo for repo-specific overrides.

    Returns:
        Tuple of (prompt_text, model_name).
    """
    # Load rules (2-tier)
    verification_rules = load_rules("contract-verification", repo_path)
    if not verification_rules:
        verification_rules = _default_contract_verification_rules()

    lines = [
        f"Verify SDLC contract compliance for PR #{pr_number} in {github_repository}.",
        "",
        "## Your Task",
        "",
        "Check that the implementation matches the SDLC contract tasks and acceptance criteria.",
        "",
        "1. **Read the contract**: `egg-contract show`",
        f"2. **Read the PR diff**: `gh pr diff {pr_number}`",
        "3. **For each task in the contract**:",
        "   - Verify the described functionality is present",
        "   - Check acceptance criteria are satisfied",
        "   - Verify linked commits relate to the task",
        "4. **Mark verified items**: Use `egg-contract` CLI to update status",
        "5. **Post findings** as a PR comment",
        "",
        verification_rules,
        "",
    ]

    lines.append("Sign all PR comments with: — Authored by egg")

    return "\n".join(lines), "opus"


def _default_contract_verification_rules() -> str:
    """Return default contract verification rules."""
    return """## Verification Rules

### Task Verification
For each task in the contract:
1. The described functionality is present in the code
2. The acceptance criteria are satisfied
3. Linked commits relate to the task
4. Tests cover the new functionality where applicable

### Phase Consistency
- All tasks in completed phases are actually implemented
- Phase status matches task completion state
- No orphaned code exists that isn't covered by any task

### Contract Integrity
- No implementation changes violate previously verified criteria
- New changes don't break existing contract compliance"""


# ---------------------------------------------------------------------------
# Conflict resolution prompt
# ---------------------------------------------------------------------------


def build_conflict_prompt(
    pr_number: int | str,
    github_repository: str,
    base_ref: str = "main",
    repo_path: Path | str | None = None,
) -> tuple[str, str]:
    """Build a prompt for resolving merge conflicts.

    Args:
        pr_number: Pull request number.
        github_repository: owner/repo string.
        base_ref: Base branch to merge from (default: main).
        repo_path: Path to target repo for repo-specific overrides.

    Returns:
        Tuple of (prompt_text, model_name).
    """
    # Load rules (2-tier)
    conflict_rules = load_rules("conflict", repo_path)
    if not conflict_rules:
        conflict_rules = _default_conflict_rules()

    # Load conventions
    conventions = load_conventions("conflict", repo_path)

    lines = [
        f"Resolve merge conflicts on PR #{pr_number} in {github_repository}.",
        "",
        "## Your Task",
        "",
        "This PR has merge conflicts with the base branch. Resolve them using "
        "git merge (not rebase).",
        "",
        f"1. **Understand the PR**: `gh pr view {pr_number}`",
        f"2. **Fetch and preview**: `git fetch origin {base_ref}` then "
        f"`git merge --no-commit origin/{base_ref}`",
        "3. **Categorize each conflict** (lock file, additive, semantic, etc.)",
        "4. **Resolve conflicts** based on type (see rules below)",
        "5. **Verify**: Run `make lint`, `make test` after resolution",
        f"6. **Commit**: `git commit -m \"Merge origin/{base_ref}: resolve conflicts\"`",
        "7. **Push**: `git push` (no --force needed)",
        "",
        "## Escalation",
        "",
        "If conflicts are too complex (semantic, security-sensitive, database migrations), "
        "abort the merge with `git merge --abort` and post a comment explaining what "
        "needs human review.",
        "",
        conflict_rules,
        "",
    ]

    if conventions:
        lines.append("## Conflict Resolution Conventions\n")
        lines.append(conventions)
        lines.append("")

    lines.append(
        "Post a summary comment on the PR listing resolved conflicts "
        "and any that required escalation."
    )
    lines.append("")
    lines.append("Sign all PR comments with: — Authored by egg")

    return "\n".join(lines), "opus"


def _default_conflict_rules() -> str:
    """Return default conflict resolution rules."""
    return """## Conflict Resolution Rules

### Lock Files (package-lock.json, yarn.lock, poetry.lock, uv.lock)
Always regenerate, never manually merge. Accept the base branch version then
regenerate with the PR's manifest file.

### Additive Changes
When both sides add different things to the same location, include both.

### Import Conflicts
Include all imports from both sides and sort them.

### Semantic Conflicts
When both sides modify the same logic differently — escalate unless clearly
complementary or one is a superset of the other.

### When to Abort
- Semantic conflicts you can't resolve confidently
- API breaking changes
- Security-sensitive code
- Database migrations
- More than 5 files with non-trivial conflicts"""


# ---------------------------------------------------------------------------
# Agent-mode design review prompt
# ---------------------------------------------------------------------------


def build_agent_design_review_prompt(
    pr_number: int | str,
    github_repository: str,
    last_review_commit: str = "",
    repo_path: Path | str | None = None,
) -> tuple[str, str]:
    """Build a prompt for agent-mode design review.

    This is a specialized review that checks PR alignment with agent-mode
    design principles rather than general code quality.

    Args:
        pr_number: Pull request number.
        github_repository: owner/repo string.
        last_review_commit: If set, focus on changes since this commit.
        repo_path: Path to target repo for repo-specific overrides.

    Returns:
        Tuple of (prompt_text, model_name).
    """
    conventions = load_conventions("review", repo_path)

    is_rereview = bool(last_review_commit)

    lines = [
        f"{'Re-review' if is_rereview else 'Review'} PR #{pr_number} in "
        f"{github_repository} for agent-mode design alignment.",
        "",
        "## Agent-Mode Design Principles",
        "",
        "This is a **specialized design review**, not a general code review. "
        "Focus exclusively on whether the changes follow agent-mode design principles.",
        "",
        "Flag these **clear** anti-patterns:",
        "",
        "1. **Excessive pre-fetching** — Baking large diffs (10KB+) or full file contents "
        "into prompts instead of letting the agent fetch what it needs",
        "2. **Structured output for humans** — Requiring JSON when output goes directly "
        "to humans rather than machines",
        "3. **Post-processing pipelines** — Scripts that parse agent output to take actions "
        "the agent could take directly",
        "4. **Rigid procedures** — Micromanaging step-by-step procedures when objectives "
        "would suffice",
        "5. **Prompt-level security** — Using instructions for constraints that should be "
        "sandbox-enforced",
        "",
        "## Your Task",
        "",
    ]

    if is_rereview:
        lines.extend([
            f"1. **Review new changes**: `git diff {last_review_commit}..HEAD`",
            f"2. **Full PR context**: `gh pr diff {pr_number}`",
            "3. **Check previous feedback was addressed**",
        ])
    else:
        lines.extend([
            f"1. **Read the PR diff**: `gh pr diff {pr_number}`",
            f"2. **Understand context**: `gh pr view {pr_number}`",
        ])

    lines.extend([
        "3. **Evaluate against the design principles above**",
        "4. **Post your review**",
        "",
        "## Review Philosophy",
        "",
        "- Only flag clear violations — do not nitpick gray areas",
        "- Explain WHY each finding is an anti-pattern and suggest alternatives",
        "- Consider the trade-offs: some pre-fetching is fine if the data is small",
        "",
    ])

    if conventions:
        lines.append("## Review Conventions\n")
        lines.append(conventions)
        lines.append("")

    lines.append("Sign your review with: — Authored by egg")

    return "\n".join(lines), "opus"


# ---------------------------------------------------------------------------
# Feedback prompt
# ---------------------------------------------------------------------------


def build_feedback_prompt(
    pr_number: int | str,
    github_repository: str,
) -> tuple[str, str]:
    """Build a prompt for addressing review feedback.

    Args:
        pr_number: Pull request number.
        github_repository: owner/repo string.

    Returns:
        Tuple of (prompt_text, model_name).
    """
    prompt = f"""Address review feedback on PR #{pr_number} in {github_repository}.

## Your Task

Review feedback was just posted on this PR. Read the feedback, understand the issues
raised, make the necessary code changes, and push your fixes.

1. **Read the feedback**:
   - Issue-level comments: `gh pr view {pr_number} --comments`
   - Formal reviews: `gh api repos/{github_repository}/pulls/{pr_number}/reviews --jq '.[] | {{user: .user.login, state: .state, body: .body}}'`
   - Line-level review comments: `gh api repos/{github_repository}/pulls/{pr_number}/comments --jq '.[] | {{path: .path, line: .line, body: .body}}'`
2. **Understand the current code**: Use `gh pr diff {pr_number}` to see the PR changes.
3. **Make fixes**: Address each piece of actionable feedback.
4. **Verify**: Run tests and linters locally before pushing (`make lint`, `make test`).
5. **Push**: Commit and push all fixes together.
6. **Reply**: If you disagree with any feedback or cannot address it, reply to the specific review comment explaining your reasoning.

## Feedback Rules

Address all actionable review feedback:

**Fix**: Correctness issues, security concerns, logic errors, missing error handling,
resource leaks, breaking changes, pattern violations.

**Respond (do not fix)**: If you disagree with feedback, post a reply explaining your
reasoning instead of making the change. Be respectful but firm.

**Skip**: Pure style suggestions that linters handle, subjective preferences without
technical justification.

## Conventions

Use git commit and git push to push fixes. If you need to respond to review feedback,
use `gh pr comment` or reply inline. Sign any comments with: — Authored by egg"""

    return prompt, "opus"


# ---------------------------------------------------------------------------
# Doc updater prompt
# ---------------------------------------------------------------------------


def build_doc_updater_prompt(
    github_repository: str,
    changed_files: str,
    commit_messages: str,
    diff_stats: str = "",
    new_files: str = "",
    related_docs: str = "",
    high_risk_flags: str = "",
    high_risk_instructions: str = "",
    commit_sha: str = "HEAD~1",
    dry_run: bool = False,
) -> tuple[str, str]:
    """Build a prompt for documentation update analysis.

    This is a more complex prompt that takes pre-computed context about
    code changes and produces instructions for the doc-updater agent.

    Args:
        github_repository: owner/repo string.
        changed_files: Newline-separated list of changed code files.
        commit_messages: Recent commit messages.
        diff_stats: Git diff stat summary.
        new_files: Newline-separated list of newly added files.
        related_docs: Docs that reference related terms.
        high_risk_flags: Space-separated high-risk doc flags.
        high_risk_instructions: Formatted instructions for flagged docs.
        commit_sha: Base commit for diff comparison.
        dry_run: If True, analyze only without creating PRs.

    Returns:
        Tuple of (prompt_text, model_name).
    """
    high_risk_step = ""
    if high_risk_flags:
        high_risk_step = (
            f"3b. **Cross-reference high-risk sections** (flagged changes detected):\n\n"
            f"{high_risk_instructions}\n"
            "    For each flagged section:\n"
            "    - Read the SOURCE file to extract the current definitions\n"
            "    - Read the TARGET doc section to check for discrepancies\n"
            "    - If they differ, update the doc to match the source"
        )

    prompt = f"""# Doc Updater Task

Analyze recent code changes and determine if documentation needs to be updated.
If updates are needed, create a PR with the changes.

## Context

Recent commits (since {commit_sha}):
```
{commit_messages}
```

Diff summary: {diff_stats}

Changed files:
```
{changed_files}
```

New files added:
```
{new_files or 'none'}
```

Docs that reference related terms (may need updating):
```
{related_docs or 'none found'}
```

High-risk doc flags (auto-detected from changed files):
```
{high_risk_flags or 'none'}
```

## Your Task

1. **Analyze the changes**: Read the changed files and understand what was modified.
   Use `git diff {commit_sha}..HEAD -- <file>` to see specific changes.
   Pay special attention to newly added files — they often introduce new features
   or capabilities that existing docs don't cover.

2. **Check documentation impact**: Determine if documentation needs updating.
   Docs need updating when:

   - **New files introduce new tools, CLIs, or components** that aren't mentioned
     in existing docs (STRUCTURE.md, architecture/README.md, index.md).
   - **New features or capabilities** that users or agents need to know about.
   - **Breaking changes** that make existing documentation incorrect.
   - **New configuration options** or API changes.
   - **Architecture changes** that affect documented system design.

   Skip updates for: internal refactoring that doesn't change interfaces,
   performance improvements, bug fixes, test-only changes, or prompt tuning.

3. **Check these structural docs** (read them, don't delegate to sub-agents for
   large files):
   - `docs/development/STRUCTURE.md` — Does it list all current directories and
     key files? Are new packages/modules missing?
   - `docs/architecture/README.md` — Does it cover the components added/changed?
   - `docs/index.md` — Are new docs or templates referenced?
   - `README.md` — Does the root README reflect the current state?

{high_risk_step}
4. **Check related docs**: The "Docs that reference related terms" list above
   shows doc files that mention concepts related to the code changes. For each
   file, read it and check whether it describes behavior, interfaces, or
   workflows that were affected by this commit.

   **Skip ADRs larger than 10KB** — these are reference material that rarely
   need updating from code changes, and reading them burns significant context.

5. **If updates are needed**:
   - Create a new branch: `egg/doc-update-<short-description>`
   - Make the documentation changes
   - Create a PR with:
     - Title: `docs: <brief description>` (under 50 chars)
     - Body: Explain what code changes prompted the doc updates
     - Add `[doc-updater]` tag at the end of the title to prevent loops

6. **If no updates are needed**:
   - Report that documentation is up to date
   - No PR needed

## Guidelines

### When docs DO need updates
- **New files added**: If new source files introduce tools, CLIs, libraries, or
  components, the project structure and architecture docs likely need updating.
- **New features**: Genuinely new capabilities users need to know about.
- **Breaking changes**: Changes that make existing documentation incorrect.
- **New configuration options**: Options users can set.
- **API changes**: New endpoints, changed parameters, removed functionality.

### When to skip doc updates
- **Internal refactoring**: Changes that don't alter interfaces or capabilities.
- **Bug fixes**: Unless the bug was documented as expected behavior.
- **Test-only changes**: New or updated tests without feature changes.
- **Prompt/config tuning**: Internal configuration that doesn't change documented
  interfaces.

### How to update docs
- **Modify existing content** rather than appending new sections.
- **Don't add new sections** unless introducing genuinely new concepts.
- **Keep it brief**: A one-line clarification is often better than a new paragraph.
- **Remove outdated content**: If behavior changed, remove or update the old description.

### General principles
- Preserve existing doc style and formatting.
- Focus on user-facing docs and architectural changes.

## PR Format (if creating one)

```
docs: Update <component> docs for <change> [doc-updater]

Update documentation to reflect changes from <commit(s)>:
- <what was updated and why>

Triggered by: <link to merged PR or commit>

Authored-by: egg
```"""

    if dry_run:
        prompt += """

## Dry Run Mode

This is a dry run. Analyze the changes and describe what documentation updates
you WOULD make, but do NOT create any branches or PRs. Just report your findings."""

    return prompt, "sonnet"
