# Reviewer Sync Guide

Two surfaces run code reviews with the same criteria and standards. When modifying
one, update the other. The only differences between them should be what's required
by their different workflows.

## The Two Reviewers

| Aspect | PR Reviewer (GitHub Action) | SDLC Reviewer (Orchestrator) |
|--------|----------------------------|------------------------------|
| **Location** | `action/build-review-prompt.sh` + `action/review-conventions.md` | `orchestrator/routes/pipelines.py` (`_build_review_prompt()`) |
| **Trigger** | PR opened/updated via GitHub Actions | SDLC pipeline review phase |
| **Output** | Posts `gh pr review` (approve / request-changes / comment) | Writes JSON verdict to `.egg-state/reviews/` (approved / needs_revision) |
| **Conventions** | External file: `action/review-conventions.md` | Inline in `_build_review_prompt()` |
| **Reviewer types** | Code only | Code, contract, agent-design, refine, plan |

## What's Shared (single source of truth)

Both reviewers read from the same files in `shared/prompts/`:

- `code-review-criteria.md` — security, correctness, robustness, design, severity classification
- `contract-review-criteria.md` — task/contract verification
- `agent-design-criteria.md` — agent-mode anti-patterns

Each reviewer has an inline fallback for when the shared file can't be loaded.
**Inline fallbacks must match the shared file content.**

## What's Intentionally Different

These differences exist because the workflows are different — not because the
review standards differ:

1. **Verdict format**: PR reviewer uses GitHub review actions (approve / request-changes).
   SDLC reviewer writes a structured JSON verdict (approved / needs_revision).
2. **Posting mechanism**: PR reviewer uses `gh pr review --body-file`.
   SDLC reviewer commits a verdict file.
3. **Reviewer types**: PR reviewer only does code review. SDLC reviewer also handles
   contract, agent-design, refine, and plan reviews.
4. **Self-authored PR handling**: PR reviewer downgrades to `--comment` for self-authored
   PRs (GitHub restriction). Not applicable to SDLC reviewer.
5. **Scope preambles**: SDLC reviewer has per-type scope preambles. PR reviewer's scope
   is implicit in the prompt structure.

## What Must Stay Aligned

When updating review behavior, ensure both surfaces reflect the change:

| Concept | PR Reviewer Location | SDLC Reviewer Location |
|---------|---------------------|------------------------|
| Review criteria | `shared/prompts/code-review-criteria.md` | Same file (shared) |
| Inline fallback criteria | `action/build-review-prompt.sh` `fetch_review_rules()` | `orchestrator/routes/pipelines.py` `_get_code_review_criteria()` |
| Quality standards (be comprehensive, specific, etc.) | `action/review-conventions.md` "Comment Quality" section | `_build_review_prompt()` inline conventions |
| Verdict classification (what's blocking vs non-blocking) | `action/review-conventions.md` "When to Approve vs Request Changes" | `_build_review_prompt()` "When to Use needs_revision vs approved" |
| Procedural review steps | `action/build-review-prompt.sh` "How to Proceed" / inline fallback "How to Review" | `_build_review_prompt()` procedural steps for code reviewer |
| Severity classification | `shared/prompts/code-review-criteria.md` (shared) | Same file (shared) |

## Modification Checklist

When changing review criteria or conventions:

- [ ] Update `shared/prompts/code-review-criteria.md` (if changing shared criteria)
- [ ] Update the inline fallback in `action/build-review-prompt.sh` `fetch_review_rules()`
- [ ] Update the inline fallback in `orchestrator/routes/pipelines.py` `_get_code_review_criteria()`
- [ ] Update `action/review-conventions.md` (if changing conventions/verdict guidance)
- [ ] Update `_build_review_prompt()` inline conventions (if changing conventions/verdict guidance)
- [ ] Verify the procedural review steps match between both surfaces
