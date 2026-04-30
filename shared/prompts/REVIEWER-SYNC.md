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
| **Reviewer types** | Code only | Code, contract, agent-design, refine, plan, **security** (CRITICAL), **concurrency** (CRITICAL) |

## What's Shared (single source of truth)

Both reviewers read `code-review-criteria.md` for code reviews. The SDLC reviewer
also reads `contract-review-criteria.md` and `agent-design-criteria.md` for its
additional reviewer types. All shared files live in `shared/prompts/`:

- `code-review-criteria.md` — security, correctness, robustness, design, severity classification (both reviewers)
- `contract-review-criteria.md` — task/contract verification (SDLC reviewer only)
- `agent-design-criteria.md` — agent-mode anti-patterns (SDLC reviewer only)
- `security-review-criteria.md` — security lens (SDLC reviewer only; **CRITICAL** per #2139, see asymmetries below)
- `concurrency-review-criteria.md` — concurrency lens (SDLC reviewer only; **CRITICAL** per #2139, see asymmetries below)

Each reviewer has an inline fallback for when the shared file can't be loaded.
**Inline fallbacks must match the shared file content.** The two new lens
files explicitly inherit from `code-review-criteria.md` (their first line is
the verbatim `Inherits from \`code-review-criteria.md\`; only lens-specific
rules below override or extend it.` header) — they extend the base rules
rather than replacing them, so the inline-fallback parity rule still applies.

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
7. **Conditional ACK (BRC only)**: Concurrent SDLC reviewers can attach a
   `--pre-merge-condition "…"` flag to an ACK when the work is correct but
   requires a merge-time human action the agents cannot perform (e.g. a
   `git mv`, a secret rotation, a cross-repo config flip). The obligation is
   persisted on the approval matrix edge and rendered as a "Pre-merge
   Obligations" section on the auto-created PR body so the merger cannot
   skim past it (#1998). PR reviewer and sequential SDLC reviewer have no
   analogue — the GHA reviewer runs after the human merger has already seen
   the PR body, and the sequential reviewer's verdict file does not feed the
   PR. A conditional ACK is **not** a soft NACK: if the producer could
   address the obligation, NACK instead.
8. **Lens reviewers (`reviewer_security`, `reviewer_concurrency`, BRC only)**:
   The SDLC orchestrator runs two CRITICAL lens reviewers alongside
   `reviewer_code` on the implement phase: `reviewer_security` (criteria in
   `shared/prompts/security-review-criteria.md`) and `reviewer_concurrency`
   (criteria in `shared/prompts/concurrency-review-criteria.md`). Both
   inherit from `code-review-criteria.md` and add lens-specific patterns
   (cross-file allowlist mismatches, handler-vs-validator path mismatches,
   uncommitted-artifact / Dockerfile-symlink mismatches, retry storms,
   BRC-protocol invariants, …). A NACK from either lens blocks consensus
   until the producer re-proposes (#2139, closing #1997). The GHA
   `egg-reviewer` has **no** lens reviewers — code review is a single pass
   at the `code-review-criteria.md` lens. The asymmetry is intentional:
   the GHA reviewer fires on a small, already-merged-style PR; the SDLC
   reviewer fires on the full implement-phase change set during a
   still-mutating pipeline.
9. **Holistic reviewer (`reviewer_code_holistic`, BRC only)**: A second
   CRITICAL code reviewer (#2126) runs alongside `reviewer_code` and
   focuses on cross-module coherence — end-to-end use case, doc↔code
   symmetry, synthetic-key/sentinel coordination, silent-fallback hunt.
   It skims the full diff once rather than verifying every line. Its
   verdict gates consensus independently of `reviewer_code`'s. Criteria
   in `shared/prompts/code-review-holistic-criteria.md`. No `action/`
   counterpart — GHA review is single-pass.

## What Must Stay Aligned

When updating review behavior, ensure both surfaces reflect the change:

| Concept | PR Reviewer Location | SDLC Reviewer Location |
|---------|---------------------|------------------------|
| Review criteria | `shared/prompts/code-review-criteria.md` | Same file (shared) |
| Inline fallback criteria | `action/build-review-prompt.sh` `fetch_review_rules()` | `orchestrator/routes/pipelines.py` `_get_code_review_criteria()` |
| Quality standards (be comprehensive, specific, etc.) | `action/review-conventions.md` "Comment Quality" section | `_build_review_prompt()` inline conventions |
| Verdict classification (what's blocking vs non-blocking) | `action/review-conventions.md` "When to Approve vs Request Changes" | `_build_review_prompt()` "When to Use needs_revision vs approved" (sequential); `_build_brc_preamble()` ACK/NACK lifecycle (concurrent) |
| Procedural review steps | `action/build-review-prompt.sh` "How to Proceed" / inline fallback "How to Review" | `_build_review_prompt()` procedural steps for code reviewer |
| Diff command (first review) | `gh pr diff` (full PR changeset) | `git diff origin/{base_branch}...HEAD` (full changeset against base) |
| Diff command (re-review / delta) | `git fetch origin ${BASE_REF}` + `git log ${LAST_REVIEW_COMMIT}..HEAD --not origin/${BASE_REF} -p` (PR-side commits only; excludes base-branch merges, see [#1758](https://github.com/jwbron/egg/issues/1758)) | `git fetch origin {base_branch}` + `git log {last_reviewed_commit}..HEAD --not origin/{base_branch} -p` (same semantics for BRC `review_cycle > 1`) |
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
- [ ] If changing conditional-ACK (`--pre-merge-condition`) behavior: update the BRC preamble example in `_build_brc_preamble()`, the CLI help text in `sandbox/egg_lib/orch_cli.py`, the `_ACK_SCHEMA` description in `sandbox/egg_agent_tools/tools/brc.py`, the `ReviewPayload.pre_merge_condition` docstring in `orchestrator/attestation_schemas.py`, the PR-body renderer `_build_pre_merge_obligations_section()` in `orchestrator/routes/pipelines.py`, the live-status renderer in `cmd_consensus_status` (`sandbox/egg_lib/orch_cli.py`) and its backing field `pre_merge_conditions` in `PeerConsensusTracker.evaluate()`, the reference doc at `docs/reference/conditional-ack.md`, the "Conditional ACK vs NACK vs Plain ACK" subsection in `shared/prompts/code-review-criteria.md`, and the content validator call site for `pre_merge_condition` in `handle_consensus_ack_signal` (`orchestrator/routes/signals.py`)
- [ ] If changing in-cycle obligation resolution (`mcp__brc__resolve_obligation`, `obligation_resolved` flag) behavior: update the matrix (`ApprovalEntry.obligation_resolved`, `mark_obligation_resolved`, the resolved-flag resets in `record_ack` / `record_nack` / `invalidate_ack`, the filter in `get_pre_merge_conditions`) in `orchestrator/approval_matrix.py`, the tracker method `handle_resolve_obligation` in `orchestrator/peer_consensus.py`, the signal handler `handle_consensus_resolve_obligation_signal` in `orchestrator/routes/signals.py`, the `CONSENSUS_OBLIGATION_RESOLVED` event in `orchestrator/events.py`, the MCP tool / handler in `sandbox/egg_agent_tools/tools/brc.py` and `sandbox/egg_agent_tools/handlers/brc.py`, the producer-side step "**RESOLVE OBLIGATIONS YOU SATISFY**" in `_build_brc_preamble()`, the "Drop obligations satisfied in-cycle" subsection in `shared/prompts/code-review-criteria.md`, and the "In-cycle resolution" sections of `docs/reference/conditional-ack.md`
- [ ] If changing the re-review diff command: update the three PR-reviewer builders (`action/build-review-prompt.sh`, `action/build-agent-mode-design-review-prompt.sh`, `action/build-contract-verification-prompt.sh`), the SDLC reviewer's `_build_review_prompt()` `is_delta_review` branch plus its Delta Review directive, and the `BASE_REF` plumbing in `.github/workflows/reusable-review.yml`. The first-review three-dot `git diff origin/<base>...HEAD` is independent of the delta path.
- [ ] If adding or modifying a lens reviewer (`reviewer_security`, `reviewer_concurrency`, or a future lens): update the lens criteria file under `shared/prompts/`, the inline fallback in `orchestrator/routes/pipelines.py` (`_get_security_review_criteria()` / `_get_concurrency_review_criteria()` or equivalent), the dispatcher `_get_review_criteria_for_type()`, the per-lens scope preamble in `_get_reviewer_scope_preamble()`, the role registration in `shared/egg_contracts/agent_roles.py`, the review-graph edges in `orchestrator/review_graph.py` (`get_default_implement_graph()`), and the lens row in this file's "Reviewer types" cell and asymmetry list above. Verify the existing `replace("reviewer_", "").replace("_", "-")` mapping in `pipelines.py` still covers the new role name without a redundant dict (#1965 pitfall guard).
