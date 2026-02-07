# Agent-Mode Design Guidelines

Guidelines for designing agent workflows in egg: when to let the agent operate freely vs. when constraints are appropriate.

**These are guidelines, not absolute rules.** The goal is balanced implementations that are maintainable, flexible, and intelligent. Use judgment—preserve useful functionality while avoiding unnecessary complexity.

## Core Principle

**The sandbox is the constraint.** The gateway sidecar *technically enforces* the security boundary—these aren't just policy rules, they're blocked at the infrastructure level: merge commands fail, force-push is rejected, branch ownership is validated, credentials are held by the gateway (not exposed to the agent). Inside those rails, the agent should be free to operate however it sees fit.

If a task needs constraints beyond what the sandbox enforces, that's a signal to improve the sandbox—not to add prompt-level restrictions.

## Default to Agent Mode

For most tasks, the right approach is: give egg a clear objective in natural language and let it use its tools.

**Good:**
```
Review PR #123. Post your review on the PR.
```

**Bad:**
```
Here are the diffs for PR #123. Output JSON with file/line/severity/comment fields.
```

## The Five Guidelines

### 1. Avoid unnecessary pre-fetching

If the agent has tool access, prefer letting it pull what it needs rather than pre-fetching large amounts of data. The problems with excessive pre-fetching:
- You decide what's relevant (you're often wrong)
- You hit size limits and truncate (losing signal)
- The agent can't explore beyond what you fetched

**What's fine to include:**
- Lightweight metadata (PR number, repo name, who asked)
- Context the agent would fetch anyway (e.g., "this is a re-review, you last reviewed at commit X")
- Small amounts of structured data that inform the task

The key question is: does this context help the agent work more effectively, or does it constrain the agent's ability to explore?

### 2. Don't specify output formats unless there's a downstream consumer

Structured output (JSON, specific templates) is appropriate when a machine needs to parse the output. It's not appropriate when the output goes directly to humans (PR comments, issue responses, review feedback).

Natural language is almost always better for human-facing output.

### 3. Post-processing pipelines are a code smell

If you're building a script to parse the agent's output and take action on it, ask: could the agent just take that action directly?

Usually yes. The agent can:
- Post GitHub reviews
- Create issues
- Push code
- Comment on PRs

It doesn't need a middleman.

### 4. Prefer *what* over *how*

Tell the agent what outcome you want. Include domain context (review guidelines, coding standards) but avoid micromanaging the procedure.

**Good:**
```
Focus on security issues and logic errors. Don't flag style issues that linters catch.
```

**Bad:**
```
For each issue, output severity as critical/warning/suggestion and
category as security/correctness/quality/standards.
```

**However:** Some procedural context is helpful when it provides information the agent can't easily discover. For example, "this is a re-review—check whether previous feedback was addressed" gives the agent useful context about the task's history.

### 5. Let the agent explore and use judgment

The agent can fetch more context if it needs it. Don't try to anticipate everything it might want to know. Provide the task and let it investigate.

If the agent decides something isn't worth commenting on, or needs a different approach than expected, that's usually fine. The security boundary prevents harm; within that boundary, let the agent make decisions.

## Applying These Guidelines

These guidelines describe a direction, not a destination. When refactoring a workflow to align with these principles:

1. **Preserve valuable functionality.** If existing code provides useful context (like re-review detection) or handles edge cases well, keep it. The goal is to remove unnecessary complexity, not strip out everything.

2. **Refactor incrementally.** Don't rewrite from scratch. Identify specific anti-patterns and address them while preserving what works.

3. **Share common infrastructure.** If multiple bots need the same functionality (fetching PR metadata, detecting re-reviews, posting reviews), factor it into shared modules. This isn't pre-fetching—it's good software engineering.

4. **Use judgment.** A workflow that's 80% aligned with these guidelines but actually works is better than one that's 100% aligned but fragile or missing functionality.

## When Constraints ARE Appropriate

Not all constraints are bad. These are legitimate reasons to restrict agent behavior:

### When you need a security boundary: extend the sandbox

Prompt-level instructions aren't security controls—agents can ignore them. If you need to enforce a security boundary:

1. **Network isolation:** Use private mode to block external network access entirely
2. **Credential isolation:** The sandbox already holds credentials in the gateway, not exposed to agents
3. **Operation blocking:** Extend the gateway sidecar to reject specific operations

Don't rely on "Don't do X" instructions for security-critical constraints. Either the sandbox enforces it technically, or you accept the risk that the agent might do it anyway.

**Example:** If an agent shouldn't post to external services, run it in private mode—don't just tell it not to.

### Hard business rules: enforce in the sidecar

Non-negotiable requirements shouldn't be prompt-level instructions—agents can ignore them. If a rule is truly non-negotiable, extend the gateway sidecar to enforce it technically:

- **PR approval blocking:** The gateway already blocks `gh pr merge`. To also block approving reviews, add a filter that rejects `gh pr review --approve` and only allows `--comment` or `--request-changes`.
- **Required PR fields:** If every PR must have a test plan, validate this in the gateway before allowing `gh pr create` to succeed.

Don't rely on "Always do X" or "Never do Y" instructions for hard business rules. Either the gateway enforces it, or accept that it's a soft guideline the agent might occasionally miss.

### Machine-readable output for genuine automation

When output actually needs to be parsed by another system (not just formatted for humans):
- CI status checks that parse specific formats
- Automated deployment triggers
- Integration with external tools

### Rate limiting to avoid noise

Reasonable limits on agent output:
- "Post at most 10 inline comments"
- "Summarize rather than commenting on every minor issue"

## Anti-Patterns vs. Good Patterns

The patterns below are problems when taken to extremes. Use judgment about when the pattern is helping vs. hindering.

### Anti-pattern 1: Pre-fetch everything

**Wrong approach:**
```python
# Fetch all the data upfront
diff = fetch_pr_diff(pr_number)
files = fetch_changed_files(pr_number)
comments = fetch_existing_comments(pr_number)
author = fetch_pr_author(pr_number)

prompt = f"""
Here is the PR diff:
{diff}

Here are the files:
{files}

Here are existing comments:
{comments}

Review this code and output JSON...
"""
```

**Right approach:**
```
Review PR #123. Focus on security and correctness issues. Post your review on the PR.
```

The agent will fetch what it needs, follow links that seem relevant, and skip what doesn't matter.

**Note:** This doesn't mean you can't provide *any* context. Telling the agent "this is PR #123" or "you last reviewed at commit abc123" is fine—it's orientation, not pre-fetching. The anti-pattern is baking in large diffs, file contents, or comment threads that constrain what the agent can see.

### Anti-pattern 2: JSON output pipeline

**Wrong approach:**
```python
response = agent.run("Review this code, output JSON with issues")
issues = json.loads(response)
for issue in issues:
    github.post_comment(issue["file"], issue["line"], issue["comment"])
```

**Right approach:**
```
Review PR #123 and post your review comments directly on GitHub.
```

The agent can call the GitHub API itself. No parsing needed.

### Good pattern: Shared infrastructure

Reusable modules for common operations are good engineering, not anti-patterns:

```bash
# Good: Shared module for detecting re-reviews
source review-bot-base.sh
last_commit=$(get_last_review_commit "$PR_NUMBER" "$BOT_NAME")
prompt="Review PR #${PR_NUMBER}."
if [[ -n "$last_commit" ]]; then
    prompt+=" This is a re-review (last at $last_commit)."
fi
```

This isn't "pre-fetching"—it's providing useful context that helps the agent do its job. The distinction: are you constraining what the agent can see, or informing it about the task?

### Good pattern: Composition over duplication

When multiple bots share functionality (fetching PR metadata, posting reviews, detecting previous reviews), extract it into reusable modules. Each specialized bot can use the shared base while adding its own focus:

```bash
# base: shared functionality all review bots need
# security-bot: uses base + security-specific rules
# design-bot: uses base + design-specific rules
```

### Generalized vs. specialized workflows

This isn't strictly an anti-pattern—it's a design choice with tradeoffs.

**Generalized workflows** give broad objectives and let the agent decide what matters:
```
Review this PR for issues that would affect correctness, security, or maintainability.
Skip style issues that automated linters handle. Post your review on the PR.
```

**Specialized workflows** focus the agent on specific concerns:
```
Review this PR specifically for SQL injection vulnerabilities. Check all database queries
for proper parameterization. Post your findings on the PR.
```

**The tradeoff:** Adding checklist items to a generalized workflow dilutes focus. Every item you add competes for attention with everything else. A 20-item checklist produces shallow coverage of everything rather than deep analysis of what matters.

**Better approach:** One generalized review bot for broad coverage, plus specialized bots for specific concerns (security audit, performance review, accessibility check). Each bot does one thing well rather than one bot doing everything shallowly.

**When checklists make sense:** Specialized workflows benefit from focused guidelines. A security review bot *should* have specific things to check—that's its purpose. The anti-pattern is overloading a generalized bot with prescriptive checklists.

## Case Study: Auto-Review Redesign

The auto-review feature (#161) exemplifies the patterns to avoid:

**Original design (wrong):**
1. Pre-fetch PR diff and bake into prompt
2. Require structured JSON output
3. Parse JSON and post to GitHub
4. Detailed instructions on what to check

**Specific problems from the original implementation:**

- **Truncation lost signal:** Diffs were truncated at 15K/file and 100K total. Large changes lost context exactly when it mattered most.
- **JSON schema caused shallow reviews:** Forcing `{"file", "line", "severity", "category", "comment"}` led to formulaic, checklist-style comments. Architectural concerns that don't map to single lines got missed entirely.
- **Brittle post-processing:** The 484-line `post-review-comments.sh` script had to parse JSON from logs and do lossy line-number-to-diff-position mapping—a problem the agent would never create if posting directly.
- **Closed-book exam:** The agent couldn't explore how changed functions were called, check test coverage, or understand architectural context. Good review requires more than staring at an isolated diff.

**Improved design (right):**
1. Tell agent: "Review PR #123, post review on GitHub"
2. Agent fetches diff itself (can fetch more context if needed)
3. Agent posts review directly
4. Agent uses judgment about what's worth commenting on

The result is simpler code, better reviews, and more adaptable behavior.

## Implementation Checklist

When designing a new agent workflow, ask:

- [ ] Am I pre-fetching large amounts of data the agent should fetch itself?
- [ ] Am I requiring structured output that could be natural language?
- [ ] Am I building post-processing to parse agent output?
- [ ] Am I over-specifying *how* instead of focusing on *what*?
- [ ] Do my constraints go beyond what the sandbox enforces?
- [ ] Would this design work if the agent needs more context than I anticipated?
- [ ] Am I preserving useful existing functionality while removing unnecessary complexity?

"Yes" to any of these is a signal to reconsider, but use judgment. Some context is helpful; the question is whether you're constraining vs. informing.

## Related

- [Issue #161](https://github.com/jwbron/egg/issues/161) — Rewrite auto-reviews (concrete instance)
- [Issue #134](https://github.com/jwbron/egg/issues/134) — AI-powered code review design
- [Issue #153](https://github.com/jwbron/egg/issues/153) — Self-improvement cycle

---

*This document provides guidelines for egg workflow design. Apply them with judgment—the goal is balanced, maintainable implementations, not rigid adherence to rules.*
