# Reviewer Sync Guide

Two surfaces run code reviews with the same criteria and standards. When modifying
one, update the other. The only differences between them should be what's required
by their different workflows.

## The Two Reviewers

| Aspect | PR Reviewer (GitHub Action) | SDLC Reviewer (Orchestrator) |
|--------|----------------------------|------------------------------|
| **Location** | `action/build-review-prompt.sh` + `action/review-conventions.md` | `orchestrator/routes/pipelines.py` (`_build_review_prompt()`) |
| **Trigger** | PR opened/updated via GitHub Actions | SDLC pipeline review phase |
| **Output** | Posts `gh pr review` (approve / request-changes / comment) | **Sequential**: JSON verdict to `.egg-state/reviews/`. **Concurrent (BRC)**: ACK/NACK `--reason` is the review output (no verdict file). |
| **Conventions** | External file: `action/review-conventions.md` | Inline in `_build_review_prompt()` |
| **Reviewer types** | Code only | Code, contract, agent-design, refine, plan |

## What's Shared (single source of truth)

Both reviewers read `code-review-criteria.md` for code reviews. The SDLC reviewer
also reads `contract-review-criteria.md` and `agent-design-criteria.md` for its
additional reviewer types. All shared files live in `shared/prompts/`:

- `code-review-criteria.md` — security, correctness, robustness, design, severity classification (both reviewers)
- `contract-review-criteria.md` — task/contract verification (SDLC reviewer only)
- `agent-design-criteria.md` — agent-mode anti-patterns (SDLC reviewer only)

Each reviewer has an inline fallback for when the shared file can't be loaded.
**Inline fallbacks must match the shared file content.**

## What's Intentionally Different

These differences exist because the workflows are different — not because the
review standards differ:

1. **Verdict format**: PR reviewer uses GitHub review actions (approve / request-changes).
   SDLC reviewer in **sequential** mode writes a structured JSON verdict
   (approved / needs_revision). SDLC reviewer in **concurrent (BRC)** mode
   delivers the full review via ACK/NACK `--reason` — no verdict file is written.
   Controlled by `_build_agent_prompt()`: when `concurrent=True`, the BRC
   preamble from `_build_brc_preamble()` is appended after the base review
   prompt from `_build_review_prompt()`. The base review prompt always includes
   verdict-file instructions; the BRC preamble overrides the output mechanism
   with ACK/NACK.
2. **Posting mechanism**: PR reviewer uses `gh pr review --body-file`.
   SDLC sequential reviewer commits a verdict file.
   SDLC concurrent reviewer uses `egg-orch consensus ack/nack --reason "..."`.
3. **Reviewer types**: PR reviewer only does code review. SDLC reviewer also handles
   contract, agent-design, refine, and plan reviews.
4. **Self-authored PR handling**: PR reviewer downgrades to `--comment` for self-authored
   PRs (GitHub restriction). Not applicable to SDLC reviewer.
5. **Scope preambles**: SDLC reviewer has per-type scope preambles. PR reviewer's scope
   is implicit in the prompt structure.
6. **Structured feedback format (BRC only)**: Concurrent SDLC reviewers get
   ACK/NACK lifecycle instructions in the BRC preamble (from `_build_brc_preamble()`),
   including substantive `--reason` requirements (≥50 chars) and `--files-reviewed`
   flags. PR reviewer and sequential SDLC reviewer structure their output organically.

## What Must Stay Aligned

When updating review behavior, ensure both surfaces reflect the change:

| Concept | PR Reviewer Location | SDLC Reviewer Location |
|---------|---------------------|------------------------|
| Review criteria | `shared/prompts/code-review-criteria.md` | Same file (shared) |
| Inline fallback criteria | `action/build-review-prompt.sh` `fetch_review_rules()` | `orchestrator/routes/pipelines.py` `_get_code_review_criteria()` |
| Quality standards (be comprehensive, specific, etc.) | `action/review-conventions.md` "Comment Quality" section | `_build_review_prompt()` inline conventions |
| Verdict classification (what's blocking vs non-blocking) | `action/review-conventions.md` "When to Approve vs Request Changes" | `_build_review_prompt()` "When to Use needs_revision vs approved" (sequential); `_build_brc_preamble()` ACK/NACK lifecycle (concurrent) |
| Procedural review steps | `action/build-review-prompt.sh` "How to Proceed" / inline fallback "How to Review" | `_build_review_prompt()` procedural steps for code reviewer |
| Diff command (initial review) | `gh pr diff` (full PR changeset) | `git diff origin/{base_branch}...HEAD` (full changeset against base) |
| Diff command (re-review / delta) | `git fetch origin ${BASE_REF}` + `git log ${LAST_REVIEW_COMMIT}..HEAD --not origin/${BASE_REF} -p` (PR-only commits since the last review — excludes base-branch merges; see [#1758](https://github.com/jwbron/egg/issues/1758)) | `git fetch origin {base_branch}` + `git log {last_reviewed_commit}..HEAD --not origin/{base_branch} -p` (same semantics; applies to both sequential re-reviews and BRC re-review cycles) |
| Thoroughness emphasis | "Find ALL issues on the first pass" (build-review-prompt.sh) | "Find ALL issues on the first pass" (`_build_review_prompt()`) |
| Severity classification | `shared/prompts/code-review-criteria.md` (shared) | Same file (shared) |

## Modification Checklist

When changing review criteria or conventions:

- [ ] Update `shared/prompts/code-review-criteria.md` (if changing shared criteria)
- [ ] Update the inline fallback in `action/build-review-prompt.sh` `fetch_review_rules()`
- [ ] Update the inline fallback in `orchestrator/routes/pipelines.py` `_get_code_review_criteria()`
- [ ] Update `action/review-conventions.md` (if changing conventions/verdict guidance)
- [ ] Update `_build_review_prompt()` inline conventions (if changing conventions/verdict guidance)
- [ ] Verify the procedural review steps match between both surfaces
- [ ] If changing verdict format: check `_build_review_prompt()` (sequential verdict JSON) and `_build_agent_prompt()` + `_build_brc_preamble()` (concurrent ACK/NACK)
- [ ] If changing ACK/NACK format guidance: update the structured format in `_build_brc_preamble()`
- [ ] If changing the re-review delta command: update all three prompt builders (`action/build-review-prompt.sh`, `action/build-agent-mode-design-review-prompt.sh`, `action/build-contract-verification-prompt.sh`) AND both delta-review code paths in `orchestrator/routes/pipelines.py:_build_review_prompt()` (the `diff_command` assignment and the "Delta Review" directive). Keep the `BASE_REF` env var (GHA) and `base_branch` kwarg (orchestrator) in sync so non-`main`-targeted PRs still get the correct `--not origin/<base>` exclusion.
