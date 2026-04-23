## Task Analysis

**Problem statement**: When /sdlc handles a phase_gate HITL decision, its Step 5 surfaces embedded draft questions upfront and captures the answers. It attaches them to the phase_gate resolution as a `context` string. But after the phase_gate clears, the orchestrator surfaces those same questions as separate choice/feedback pending_decisions one by one, and the skill re-prompts the user for each — because its choice/feedback handlers have no memory of the earlier answers. The user ends up answering every question twice.

**Source context**: Issue #1905 (state: OPEN, no comments) was filed from inside /sdlc 1901 when the refiner registered 5 choice decisions + 1 feedback group. Only the phase_gate (decision-1) appeared in the initial pending_decisions; decisions 2–6 were embedded in the analysis as `<!-- egg-hitl-decision id=decision-N -->` markers. The skill correctly surfaced them upfront (via Step 5) and attached answers to the phase_gate resolution's `context`. After the phase_gate resolved, decisions 2–6 appeared one-by-one with identical questions, each re-prompted. The issue explicitly recommends Option 1 (skill-only); Option 2 (orchestrator-side routing via _parse_resolution) is called out as a protocol change and non-goal.

**Workarounds**: None — users currently re-answer every question.

**System context**:
- Canonical skill file: `skills/sdlc/SKILL.md` (74,723 bytes). `~/.claude/skills/sdlc/SKILL.md` is an identical deployed copy (`diff -q` clean); the repo copy is the source of truth.
- Phase 4 (HITL) sits in the Full Flow only — the Short Flow runs with `hitl_gates: false`, so phase_gate decisions don't appear and this bug doesn't manifest there.
- phase_gate handling (SKILL.md:589–715):
  - Step 5 (632–656) identifies draft-embedded questions, runs AskUserQuestion, builds a `## Resolved Questions` display block.
  - Step 5 already has a "Deduplication" paragraph (641) that checks draft questions against pending_decisions in the current batch — but the whole bug is that choice/feedback decisions don't appear in the current batch; they register after the phase_gate clears.
  - Step 7a (670) folds the Resolved Questions block into the phase_gate `context` string. The orchestrator preserves it in the raw resolution but does not route it to downstream decisions (explicitly noted in the SKILL.md comment and confirmed by issue #1905).
- choice decision handler (717–733): unconditionally calls AskUserQuestion on the decision's question + options, then provide_input with `{"action": "select", "selected": "..."}`.
- feedback decision handler (735–749): unconditionally prompts per question (up to 4 per AskUserQuestion call), then provide_input with `{"action": "submit_feedback", "answers": {...}}`.
- The fix is purely skill-side: the skill must retain an in-memory map of `{normalized question text → answer}` built from Step 5 and consult it in the choice/feedback handlers before prompting.

**Technical root cause**: The skill has no persistent "already-answered" state across decision iterations / poll cycles. Step 7a writes answers into the phase_gate's `context` string (which the orchestrator does not route anywhere), but there's no in-memory map the subsequent choice/feedback handlers can consult. Step 5's existing "Deduplication" check only covers same-batch collisions — it doesn't survive into future polls where the follow-up decisions actually register.

**Files affected**:
- `skills/sdlc/SKILL.md` — Phase 4 (Full Flow) HITL handling:
  - Add a new `### Resolved Questions Map` subsection at the start of Phase 4 defining the session-scoped `{normalized_question_text → answer}` dict and its normalization rules (lowercase + strip).
  - Step 5 of the phase_gate handler: explicitly populate the map alongside the existing Resolved Questions display block.
  - choice handler (717–733): consult the map before prompting; auto-resolve on question+option match with a user-visible note; fall through to prompting otherwise.
  - feedback handler (735–749): consult the map per question; prefill matches, present only unmatched; submit a single merged provide_input; display a user-visible note.

**Risks / edge cases**:
- Question rephrasing: refiner may rewrite the question between the draft and the registered decision. Normalized-exact match will miss; the skill falls back to prompting (correct behavior — explicit non-goal in the issue).
- Answer not in options: captured answer was free-text ("Other") but the registered choice decision has a fixed option list that doesn't include it. The skill must fall back to prompting rather than force an invalid selection.
- Partial feedback matches: a feedback group may have some questions answered and others not. The handler must fill the known answers and prompt only for the unknown ones, then merge before a single provide_input call.
- Multiple phase_gates: the map accumulates across gates — newer answers for duplicate question text should overwrite older ones. Clearing the map isn't necessary; a pipeline run is short-lived in-session state.
- Transparency: the issue explicitly requires "A message is logged/displayed to the user summarizing which decisions were auto-resolved so they can verify." The handlers must print a one-line note per auto-resolved decision.
- Normalization: case-insensitive + whitespace-trimmed match only; avoid aggressive fuzzy matching (punctuation differences etc.) — too-permissive matching risks silently wrong auto-answers.