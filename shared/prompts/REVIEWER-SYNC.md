# Reviewer Sync Guide

Three surfaces run code reviews with the same criteria and standards. When modifying
one, update the others. The only differences between them should be what's required
by their different workflows.

## The Three Reviewers

| Aspect | Babysit-PR Reviewer | SDLC Reviewer (Orchestrator) | Babysit-PR Pipeline Reviewer |
|--------|---------------------|------------------------------|------------------------------|
| **Location** | `shared/egg_babysit/prompts.py` (`build_review_prompt()`) | `orchestrator/routes/pipelines.py` (`_build_review_prompt()`) | Same as Babysit-PR Reviewer |
| **Trigger** | `on-push-babysit.yml` pipeline on PR push | SDLC pipeline review phase | Concurrent BRC mode during babysit cycle |
| **Output** | Posts `gh pr review` (approve / request-changes / comment) | Writes JSON verdict to `.egg-state/reviews/` (approved / needs_revision) | Same as Babysit-PR Reviewer |
| **Review domains** | Code + conditional contract verification + conditional agent-mode design | Code, contract, agent-design, refine, plan | Same as Babysit-PR Reviewer |

> **Note:** The PR Reviewer previously lived in `action/build-review-prompt.sh` (GHA workflow). This has been replaced by `shared/egg_babysit/prompts.py`, which consolidates all three review domains (base code, contract verification, agent-mode design) into a single reviewer with conditional inclusion.

## What's Shared (single source of truth)

All reviewers read from `shared/prompts/`:

- `code-review-criteria.md` — security, correctness, robustness, design, severity classification (all reviewers)
- `contract-review-criteria.md` — task/contract verification (babysit-pr reviewer when `sdlc:pr` label present; SDLC reviewer)
- `agent-design-criteria.md` — agent-mode anti-patterns (babysit-pr reviewer when agent-related files changed; SDLC reviewer)

Each reviewer has an inline fallback for when the shared file can't be loaded.
**Inline fallbacks must match the shared file content.**

## What's Intentionally Different

These differences exist because the workflows are different — not because the
review standards differ:

1. **Verdict format**: Babysit-PR reviewer uses GitHub review actions (approve / request-changes).
   SDLC reviewer writes a structured JSON verdict (approved / needs_revision).
2. **Posting mechanism**: Babysit-PR reviewer uses `gh pr review --body-file`.
   SDLC reviewer commits a verdict file.
3. **Reviewer types**: Babysit-PR reviewer combines code, contract, and design reviews
   into a single pass with conditional criteria. SDLC reviewer handles these as separate
   reviewer types.
4. **Self-authored PR handling**: Babysit-PR reviewer downgrades to `--comment` for self-authored
   PRs (GitHub restriction). Not applicable to SDLC reviewer.
5. **Scope preambles**: SDLC reviewer has per-type scope preambles. Babysit-PR reviewer's
   scope is implicit in the prompt structure.
6. **BRC consensus**: In concurrent mode, the babysit-PR reviewer participates in BRC
   consensus (ACK/NACK) with the fixer agent. SDLC reviewer does not use BRC.

## What Must Stay Aligned

When updating review behavior, ensure all surfaces reflect the change:

| Concept | Babysit-PR Reviewer Location | SDLC Reviewer Location |
|---------|------------------------------|------------------------|
| Review criteria | `shared/prompts/code-review-criteria.md` | Same file (shared) |
| Inline fallback criteria | `shared/egg_babysit/prompts.py` `build_review_prompt()` | `orchestrator/routes/pipelines.py` `_get_code_review_criteria()` |
| Quality standards (be comprehensive, specific, etc.) | `shared/egg_babysit/prompts.py` inline conventions | `_build_review_prompt()` inline conventions |
| Verdict classification (what's blocking vs non-blocking) | `shared/egg_babysit/prompts.py` verdict guidance | `_build_review_prompt()` "When to Use needs_revision vs approved" |
| Procedural review steps | `shared/egg_babysit/prompts.py` review procedure | `_build_review_prompt()` procedural steps for code reviewer |
| Severity classification | `shared/prompts/code-review-criteria.md` (shared) | Same file (shared) |

## Modification Checklist

When changing review criteria or conventions:

- [ ] Update `shared/prompts/code-review-criteria.md` (if changing shared criteria)
- [ ] Update the inline fallback in `shared/egg_babysit/prompts.py` `build_review_prompt()`
- [ ] Update the inline fallback in `orchestrator/routes/pipelines.py` `_get_code_review_criteria()`
- [ ] Update inline conventions in `shared/egg_babysit/prompts.py` (if changing conventions/verdict guidance)
- [ ] Update `_build_review_prompt()` inline conventions (if changing conventions/verdict guidance)
- [ ] Verify the procedural review steps match between both surfaces
