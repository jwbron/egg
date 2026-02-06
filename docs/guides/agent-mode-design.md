# Agent-Mode Design Guidelines

Guidelines for designing agent workflows in egg: when to let the agent operate freely vs. when constraints are appropriate.

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

### 1. Pre-fetching is usually wrong

If the agent has tool access, don't pre-fetch data for it. Let it pull what it needs. Pre-fetching means:
- You decide what's relevant (you're often wrong)
- You hit size limits and truncate (losing signal)
- The agent can't explore beyond what you fetched

**Exception:** Lightweight metadata (PR number, repo name, who asked) is fine as orientation context.

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

### 4. Instructions should specify *what*, not *how*

Tell the agent what outcome you want, not how to achieve it. Include domain context (review guidelines, coding standards) but not procedural instructions.

**Good:**
```
Focus on security issues and logic errors. Don't flag style issues that linters catch.
```

**Bad:**
```
For each issue, output severity as critical/warning/suggestion and
category as security/correctness/quality/standards.
```

### 5. Let the agent explore and use judgment

The agent can fetch more context if it needs it. Don't try to anticipate everything it might want to know. Provide the task and let it investigate.

If the agent decides something isn't worth commenting on, or needs a different approach than expected, that's usually fine. The security boundary prevents harm; within that boundary, let the agent make decisions.

## When Constraints ARE Appropriate

Not all constraints are bad. These are legitimate reasons to restrict agent behavior:

### Security boundaries the sandbox doesn't cover

If there's a security concern the gateway doesn't enforce, add explicit instructions:
- "Don't post to external services"
- "Don't include credentials in PR descriptions"

### Hard business rules

Non-negotiable requirements that the agent shouldn't decide on its own:
- "Never approve PRs automatically, only COMMENT reviews"
- "Always include a test plan in PR descriptions"

### Machine-readable output for genuine automation

When output actually needs to be parsed by another system (not just formatted for humans):
- CI status checks that parse specific formats
- Automated deployment triggers
- Integration with external tools

### Rate limiting to avoid noise

Reasonable limits on agent output:
- "Post at most 10 inline comments"
- "Summarize rather than commenting on every minor issue"

## Anti-Patterns to Avoid

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

### Anti-pattern 3: Prescriptive checklist

**Wrong approach:**
```
Review this PR. Check for:
1. Are all functions documented?
2. Are there any TODO comments?
3. Is error handling present?
4. Are imports sorted?
...
```

**Right approach:**
```
Review this PR for issues that would affect correctness, security, or maintainability.
Skip style issues that automated linters handle. Post your review on the PR.
```

Let the agent use judgment about what matters.

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

- [ ] Am I pre-fetching data the agent could fetch itself?
- [ ] Am I requiring structured output that could be natural language?
- [ ] Am I building post-processing to parse agent output?
- [ ] Am I specifying *how* instead of *what*?
- [ ] Do my constraints go beyond what the sandbox enforces?
- [ ] Would this design work if the agent needs more context than I anticipated?

If you answer "yes" to any of these, reconsider the design.

## Related

- [Issue #161](https://github.com/jwbron/egg/issues/161) — Rewrite auto-reviews (concrete instance)
- [Issue #134](https://github.com/jwbron/egg/issues/134) — AI-powered code review design
- [Issue #153](https://github.com/jwbron/egg/issues/153) — Self-improvement cycle

---

*This document establishes principles for egg workflow design. Consult it when building new agent-driven features.*
