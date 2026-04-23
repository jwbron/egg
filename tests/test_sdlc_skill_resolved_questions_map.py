"""
Structural test for `skills/sdlc/SKILL.md` changes from issue #1905.

This is the canonical skill file — a markdown behavior spec interpreted by
the `/sdlc` skill at runtime. Issue #1905 added a session-scoped
`resolved_questions_map` so draft-embedded answers captured during a
`phase_gate` are reused when the orchestrator later registers the same
questions as standalone `choice` / `feedback` decisions.

There is no runtime code to exercise (the skill executes inside Claude
Code), so these tests lock in the **structural** shape of the spec:

* the `### Resolved Questions Map` subsection exists at the top of Phase 4
  with the lowercase-plus-strip normalization rule spelled out
  (task-1-1)
* Step 5 of the phase_gate handler explicitly instructs the reader to
  populate `resolved_questions_map` alongside the existing Resolved
  Questions display block (task-1-1)
* the `### For choice type decisions:` section begins with a "Before
  prompting" paragraph covering lookup, option-compatibility check,
  `provide_input` payload, fall-through, and the user-visible
  auto-resolution note (task-1-2)
* the `### For feedback type decisions:` section begins with a "Before
  prompting" paragraph covering per-question lookup, partial-match
  merging, all-matched fast path, single merged `provide_input` call,
  and the user-visible auto-resolution note (task-1-3)

If the refiner later rewrites the skill and drops any of these elements,
these tests should fail with a precise pointer to the missing element.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = PROJECT_ROOT / "skills" / "sdlc" / "SKILL.md"


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"Expected SKILL.md at {SKILL_PATH}"
    return SKILL_PATH.read_text(encoding="utf-8")


def _section(text: str, header: str, *, stop_at: tuple[str, ...] = ("### ", "## ")) -> str:
    """Return the substring from `header` up to the next header at the same or higher level.

    Matches the literal header on its own line. Lines inside fenced code blocks
    (``` ... ```) are ignored when searching for the stop header — SKILL.md
    embeds literal ``` ### ... ``` fences as formatting examples, and those
    must not be treated as section boundaries.

    Raises AssertionError if `header` is not found.
    """
    idx = text.find(header)
    assert idx != -1, f"header not found in SKILL.md: {header!r}"
    rest = text[idx + len(header) :]
    end = len(rest)
    cursor = 0
    in_fence = False
    # Skip the newline immediately after the header we matched.
    if rest.startswith("\n"):
        cursor = 1
    while cursor < len(rest):
        nl = rest.find("\n", cursor)
        line_end = nl if nl != -1 else len(rest)
        line = rest[cursor:line_end]
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and any(stripped.startswith(s) for s in stop_at):
            end = cursor
            break
        if nl == -1:
            break
        cursor = nl + 1
    return header + rest[:end]


# ---------------------------------------------------------------------------
# task-1-1: Resolved Questions Map subsection + Step 5 update
# ---------------------------------------------------------------------------


class TestResolvedQuestionsMapSection:
    def test_subsection_header_exists(self, skill_text: str) -> None:
        assert "### Resolved Questions Map" in skill_text, (
            "task-1-1: missing `### Resolved Questions Map` subsection header"
        )

    def test_subsection_appears_before_phase_gate_handler(self, skill_text: str) -> None:
        map_idx = skill_text.find("### Resolved Questions Map")
        gate_idx = skill_text.find("### For `phase_gate` decisions")
        assert map_idx != -1 and gate_idx != -1
        assert map_idx < gate_idx, (
            "task-1-1: `### Resolved Questions Map` must precede "
            "`### For phase_gate decisions:` so the definition is in scope when readers "
            "reach the handler"
        )

    def test_subsection_names_the_identifier(self, skill_text: str) -> None:
        section = _section(skill_text, "### Resolved Questions Map")
        assert "`resolved_questions_map`" in section, (
            "task-1-1: subsection must name `resolved_questions_map` (backticked "
            "identifier) so handler references are anchored"
        )

    def test_subsection_documents_normalization_rule(self, skill_text: str) -> None:
        section = _section(skill_text, "### Resolved Questions Map").lower()
        # Accept the canonical `question.strip().lower()` form or an equivalent
        # description ("lowercase" + "strip" / "trim").
        has_canonical = ".strip().lower()" in section
        has_described = ("lowercase" in section) and ("strip" in section or "trim" in section)
        assert has_canonical or has_described, (
            "task-1-1: subsection must spell out the normalization rule "
            "(strip + lowercase) — either as `question.strip().lower()` or a "
            "described equivalent"
        )

    def test_subsection_marks_map_as_session_scoped(self, skill_text: str) -> None:
        section = _section(skill_text, "### Resolved Questions Map").lower()
        assert "session" in section, (
            "task-1-1: subsection must describe the map as session-scoped so "
            "readers know it persists across decisions but not runs"
        )

    def test_step_5_mentions_populating_the_map(self, skill_text: str) -> None:
        # Step 5 sits inside `### For phase_gate decisions:`. We pull that whole
        # section and verify the instruction to populate the map is present.
        section = _section(skill_text, "### For `phase_gate` decisions")
        assert "resolved_questions_map" in section, (
            "task-1-1: Step 5 (inside `### For phase_gate decisions:`) must "
            "instruct the reader to populate `resolved_questions_map` — the "
            "identifier is missing from the phase_gate handler"
        )
        # The instruction should sit near the Resolved Questions display block,
        # not buried somewhere unrelated.
        lowered = section.lower()
        display_idx = lowered.find("resolved questions")
        map_idx = lowered.find("resolved_questions_map")
        assert display_idx != -1 and map_idx != -1
        # The map mention should be close to (within 2000 chars of) the display
        # block — same step, not a stray reference later.
        assert abs(map_idx - display_idx) < 2000, (
            "task-1-1: the `resolved_questions_map` population instruction "
            "must live in Step 5 alongside the Resolved Questions display "
            "block, not elsewhere in the handler"
        )


# ---------------------------------------------------------------------------
# task-1-2: choice handler — "Before prompting" paragraph
# ---------------------------------------------------------------------------


class TestChoiceHandlerBeforePrompting:
    @pytest.fixture(scope="class")
    def section(self, skill_text: str) -> str:
        return _section(skill_text, "### For `choice` type decisions:")

    def test_section_exists(self, section: str) -> None:
        assert section.startswith("### For `choice` type decisions:")

    def test_before_prompting_paragraph_present(self, section: str) -> None:
        assert "Before prompting" in section, (
            "task-1-2: choice handler must begin with a `Before prompting` "
            "paragraph documenting the captured-context lookup"
        )

    def test_paragraph_is_prepended(self, skill_text: str, section: str) -> None:
        """`Before prompting` must appear before the legacy prompt-flow
        instructions (the `AskUserQuestion` call and `"action": "select"`
        payload) so readers consult the map first."""
        bp_idx = section.find("Before prompting")
        # The legacy prompt flow starts at `AskUserQuestion` / the canonical
        # `{"action": "select", "selected": "<chosen option text>"}` example.
        legacy_idx_1 = section.find("AskUserQuestion")
        legacy_idx_2 = section.find('"<chosen option text>"')
        assert bp_idx != -1
        assert legacy_idx_1 != -1, "choice handler lost its AskUserQuestion prompt flow"
        assert legacy_idx_2 != -1, (
            "choice handler lost its `<chosen option text>` prompt-flow example"
        )
        assert bp_idx < legacy_idx_1 < legacy_idx_2, (
            "task-1-2: the `Before prompting` paragraph must be prepended to "
            "the choice handler — it currently appears after the legacy "
            "prompt-flow instructions"
        )

    def test_references_resolved_questions_map(self, section: str) -> None:
        assert "resolved_questions_map" in section, (
            "task-1-2: choice handler must reference `resolved_questions_map` to perform the lookup"
        )

    def test_option_compatibility_check_documented(self, section: str) -> None:
        lowered = section.lower()
        assert "decision.options" in section or "options" in lowered, (
            "task-1-2: choice handler must describe comparing the stored "
            "answer against `decision.options`"
        )
        # The same normalization must apply on the option side.
        has_canonical = ".strip().lower()" in section
        has_described = ("normaliz" in lowered) and ("option" in lowered)
        assert has_canonical or has_described, (
            "task-1-2: choice handler must describe normalizing options the "
            "same way (`.strip().lower()` or an explicit 'normalize' step)"
        )

    def test_auto_submit_payload_documented(self, section: str) -> None:
        assert '"action": "select"' in section, (
            'task-1-2: auto-resolution must submit `{"action": "select", ...}` via provide_input'
        )
        assert '"selected"' in section, (
            "task-1-2: the auto-submit payload must include a `selected` field"
        )
        assert "provide_input" in section, (
            "task-1-2: choice handler must name the `provide_input` tool call "
            "that carries the auto-resolution"
        )

    def test_auto_resolved_note_format(self, section: str) -> None:
        # The note wording from the contract: "Auto-resolved <decision_id>:
        # selected '<option>' from captured context."
        assert "Auto-resolved" in section, (
            "task-1-2: choice handler must document a user-visible note "
            "starting with `Auto-resolved` so the user can catch bad matches"
        )
        assert "captured context" in section, (
            "task-1-2: the auto-resolution note must attribute the source as 'captured context'"
        )

    def test_fall_through_on_no_match_documented(self, section: str) -> None:
        lowered = section.lower()
        has_fallthrough = (
            "fall through" in lowered or "fall back" in lowered or "fallback" in lowered
        )
        assert has_fallthrough, (
            "task-1-2: choice handler must document fall-through to the "
            "prompt flow when the stored answer doesn't match any option"
        )


# ---------------------------------------------------------------------------
# task-1-3: feedback handler — "Before prompting" paragraph
# ---------------------------------------------------------------------------


class TestFeedbackHandlerBeforePrompting:
    @pytest.fixture(scope="class")
    def section(self, skill_text: str) -> str:
        return _section(skill_text, "### For `feedback` type decisions:")

    def test_section_exists(self, section: str) -> None:
        assert section.startswith("### For `feedback` type decisions:")

    def test_before_prompting_paragraph_present(self, section: str) -> None:
        assert "Before prompting" in section, (
            "task-1-3: feedback handler must begin with a `Before prompting` "
            "paragraph documenting per-question lookup"
        )

    def test_paragraph_is_prepended(self, section: str) -> None:
        """`Before prompting` must appear before the legacy prompt-flow
        instructions (the per-question grouping rules / canonical
        submit_feedback payload)."""
        bp_idx = section.find("Before prompting")
        # Legacy: `Feedback decisions include a questions array` is the first
        # sentence of the original paragraph that still lives after the new one.
        legacy_idx_1 = section.find("Feedback decisions include a `questions`")
        legacy_idx_2 = section.find('"submit_feedback"')
        assert bp_idx != -1
        assert legacy_idx_1 != -1, "feedback handler lost its legacy `questions array` paragraph"
        assert legacy_idx_2 != -1
        assert bp_idx < legacy_idx_1, (
            "task-1-3: the `Before prompting` paragraph must be prepended to "
            "the feedback handler — it currently appears after the legacy "
            "prompt-flow instructions"
        )

    def test_references_resolved_questions_map(self, section: str) -> None:
        assert "resolved_questions_map" in section, (
            "task-1-3: feedback handler must reference `resolved_questions_map` "
            "to perform per-question lookup"
        )

    def test_per_question_lookup_documented(self, section: str) -> None:
        lowered = section.lower()
        # Must describe iterating questions and keying answers by id/q-<n>.
        assert "for each" in lowered or "each entry" in lowered, (
            "task-1-3: feedback handler must describe iterating the "
            "`questions` array question-by-question"
        )
        assert "q-" in section, (
            "task-1-3: feedback handler must describe the `q-<1-based index>` "
            "fallback key for questions missing an `id`"
        )

    def test_prefilled_answers_and_unmatched_list_present(self, section: str) -> None:
        lowered = section.lower()
        assert "prefilled" in lowered, (
            "task-1-3: feedback handler must describe a prefilled-answers "
            "collection built from the captured-context matches"
        )
        assert "unmatched" in lowered, (
            "task-1-3: feedback handler must describe tracking unmatched "
            "questions separately so only those get prompted"
        )

    def test_all_matched_fast_path_documented(self, section: str) -> None:
        lowered = section.lower()
        # Accept any of the contract phrasings for the fast path.
        has_fast_path = (
            "all-matched" in lowered
            or "all matched" in lowered
            or "fast path" in lowered
            or "skip `askuserquestion`" in lowered
            or "skip askuserquestion" in lowered
        )
        assert has_fast_path, (
            "task-1-3: feedback handler must document the all-matched "
            "fast path that skips `AskUserQuestion` entirely when every "
            "question was already answered"
        )

    def test_single_merged_submit_documented(self, section: str) -> None:
        lowered = section.lower()
        # A single merged provide_input call after partial-match merging.
        assert "merge" in lowered or "merged" in lowered, (
            "task-1-3: feedback handler must describe merging prefilled "
            "answers with newly-collected answers"
        )
        assert '"action": "submit_feedback"' in section, (
            'task-1-3: the merged submission must carry `{"action": "submit_feedback", ...}`'
        )
        assert "provide_input" in section, (
            "task-1-3: the merged submission must go through `provide_input`"
        )

    def test_auto_resolved_note_present(self, section: str) -> None:
        assert "Auto-resolved" in section, (
            "task-1-3: feedback handler must document a user-visible "
            "`Auto-resolved ...` note naming the decision ID and the "
            "question IDs that were prefilled from captured context"
        )
        assert "captured context" in section, (
            "task-1-3: the auto-resolution note must attribute the source as 'captured context'"
        )


# ---------------------------------------------------------------------------
# Cross-cutting: the skill file itself remains well-formed
# ---------------------------------------------------------------------------


class TestSkillFileIntegrity:
    def test_file_is_nonempty(self, skill_text: str) -> None:
        assert len(skill_text) > 1000, (
            "SKILL.md unexpectedly tiny — check for accidental truncation"
        )

    def test_phase_4_header_still_present(self, skill_text: str) -> None:
        assert "## Phase 4 — HITL" in skill_text, (
            "SKILL.md lost the `## Phase 4 — HITL` header — the new subsections "
            "must live inside Phase 4"
        )

    def test_existing_handlers_still_present(self, skill_text: str) -> None:
        # Smoke: the three handler sections touched by this change must all
        # still exist.
        for header in (
            "### For `phase_gate` decisions",
            "### For `choice` type decisions:",
            "### For `feedback` type decisions:",
        ):
            assert header in skill_text, f"SKILL.md lost handler header: {header!r}"
