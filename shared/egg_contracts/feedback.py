"""
Feedback comment handling for SDLC pipeline.

Provides functionality for:
- Generating markdown feedback comments with questions for humans
- Parsing human-edited feedback comments to extract answers
- Handling debounce timing to prevent accidental rapid submissions

The feedback comment consolidates all open-ended questions into a single editable
comment. Humans edit the comment to fill in answers and check a "Submit feedback"
checkbox to trigger processing.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .hitl import DEFAULT_DEBOUNCE_SECONDS


@dataclass
class FeedbackQuestionInput:
    """Input for creating a feedback question."""

    id: str
    question: str


@dataclass
class ParsedFeedbackResponse:
    """Parsed response from a feedback comment."""

    feedback_id: str
    questions: dict[str, str | None]  # question_id -> answer (None if unanswered)
    submitted: bool
    raw_comment: str

    def get_answer(self, question_id: str) -> str | None:
        """Get the answer for a specific question."""
        return self.questions.get(question_id)

    def has_all_answers(self) -> bool:
        """Check if all questions have been answered."""
        return all(answer is not None and answer.strip() for answer in self.questions.values())

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "feedback_id": self.feedback_id,
            "questions": self.questions,
            "submitted": self.submitted,
        }


# Marker patterns
FEEDBACK_MARKER_PATTERN = re.compile(r"<!--\s*egg-feedback\s+id=([a-z0-9-]+)\s*-->")
QUESTION_HEADER_PATTERN = re.compile(r"\*\*Q(\d+):\s*(.+?)\*\*")
SUBMIT_CHECKBOX_PATTERN = re.compile(r"-\s*\[([ xX])\]\s*Submit feedback", re.IGNORECASE)


def generate_feedback_id(existing_ids: list[str] | None = None) -> str:
    """
    Generate a unique feedback ID.

    Args:
        existing_ids: List of existing feedback IDs to avoid collision

    Returns:
        A unique feedback ID like "feedback-1", "feedback-2", etc.
    """
    if not existing_ids:
        return "feedback-1"

    # Extract numbers from existing IDs
    numbers = []
    for fid in existing_ids:
        match = re.match(r"feedback-(\d+)", fid)
        if match:
            numbers.append(int(match.group(1)))

    next_num = max(numbers) + 1 if numbers else 1
    return f"feedback-{next_num}"


def generate_feedback_comment(
    feedback_id: str,
    questions: list[FeedbackQuestionInput],
    include_debounce_notice: bool = False,
) -> str:
    """
    Generate a markdown feedback comment for humans to edit.

    Args:
        feedback_id: Unique identifier for this feedback (e.g., "feedback-1")
        questions: List of questions to include
        include_debounce_notice: Whether to include debounce notice

    Returns:
        Markdown string with the feedback comment structure
    """
    lines = [
        f"<!-- egg-feedback id={feedback_id} -->",
        "",
        "## Questions & Feedback",
        "",
        "Please **edit this comment** to answer questions or provide feedback.",
        "When you're done, check the box below to submit.",
        "",
        "---",
        "",
        "### Open Questions",
        "",
    ]

    for q in questions:
        lines.extend(
            [
                f"**{q.id}: {q.question}**",
                "",
                "> _Your answer here_",
                "",
            ]
        )

    lines.extend(
        [
            "---",
            "",
            "### Additional Feedback (optional)",
            "",
            "> _Add any other feedback or context here_",
            "",
            "---",
            "",
            "- [ ] Submit feedback (I'm done editing)",
            "",
        ]
    )

    if include_debounce_notice:
        lines.extend(
            [
                "---",
                "",
                _generate_feedback_debounce_notice(DEFAULT_DEBOUNCE_SECONDS),
                "",
            ]
        )

    lines.extend(
        [
            "---",
            "",
            "*Authored-by: egg*",
        ]
    )

    return "\n".join(lines)


def _generate_feedback_debounce_notice(seconds_remaining: int) -> str:
    """Generate a debounce countdown notice for feedback."""
    if seconds_remaining <= 0:
        return "> Processing feedback..."

    return (
        f"> **Debounce active:** Feedback will be processed in **{seconds_remaining} seconds**.\n"
        "> \n"
        "> _Making additional changes will reset the timer._"
    )


def parse_feedback_comment(comment_body: str) -> ParsedFeedbackResponse | None:
    """
    Parse a feedback comment to extract the feedback ID, answers, and submit state.

    Args:
        comment_body: The raw comment body

    Returns:
        ParsedFeedbackResponse if the comment contains a valid feedback marker,
        None otherwise
    """
    # Check for feedback marker
    marker_match = FEEDBACK_MARKER_PATTERN.search(comment_body)
    if not marker_match:
        return None

    feedback_id = marker_match.group(1)

    # Parse questions and answers
    questions: dict[str, str | None] = {}

    # Find all question headers and their answers
    # Questions are in format: **Q1: Question text?**
    # Answers follow in blockquotes: > Answer text
    lines = comment_body.split("\n")
    current_question_id: str | None = None
    current_answer_lines: list[str] = []
    in_answer_block = False

    for line in lines:
        # Check for question header
        q_match = QUESTION_HEADER_PATTERN.search(line)
        if q_match:
            # Save previous question's answer if exists
            if current_question_id is not None:
                answer = _extract_answer(current_answer_lines)
                questions[current_question_id] = answer

            current_question_id = f"Q{q_match.group(1)}"
            current_answer_lines = []
            in_answer_block = False
            continue

        # Check for answer blockquote (starts with >)
        if current_question_id is not None:
            stripped = line.strip()
            if stripped.startswith(">"):
                in_answer_block = True
                # Extract content after >
                content = stripped[1:].strip()
                current_answer_lines.append(content)
            elif in_answer_block and stripped == "":
                # Empty line after blockquote ends the answer
                in_answer_block = False
            elif stripped.startswith("---") or stripped.startswith("###"):
                # Section separator ends the answer
                if current_question_id is not None:
                    answer = _extract_answer(current_answer_lines)
                    questions[current_question_id] = answer
                    current_question_id = None
                    current_answer_lines = []
                    in_answer_block = False

    # Save last question's answer
    if current_question_id is not None:
        answer = _extract_answer(current_answer_lines)
        questions[current_question_id] = answer

    # Check submit checkbox
    submit_match = SUBMIT_CHECKBOX_PATTERN.search(comment_body)
    submitted = submit_match is not None and submit_match.group(1).lower() == "x"

    return ParsedFeedbackResponse(
        feedback_id=feedback_id,
        questions=questions,
        submitted=submitted,
        raw_comment=comment_body,
    )


def _extract_answer(answer_lines: list[str]) -> str | None:
    """
    Extract an answer from collected blockquote lines.

    Args:
        answer_lines: Lines from the blockquote (without > prefix)

    Returns:
        The answer text, or None if it's a placeholder
    """
    if not answer_lines:
        return None

    # Join lines and strip
    answer = " ".join(answer_lines).strip()

    # Check for placeholder text
    placeholder_patterns = [
        r"^_Your answer here_$",
        r"^Your answer here$",
        r"^_Add any other feedback or context here_$",
        r"^\s*$",
    ]

    for pattern in placeholder_patterns:
        if re.match(pattern, answer, re.IGNORECASE):
            return None

    return answer if answer else None


def calculate_feedback_debounce_remaining(debounce_until: datetime | None) -> int:
    """
    Calculate seconds remaining in feedback debounce period.

    Args:
        debounce_until: The debounce expiration time

    Returns:
        Seconds remaining (0 if expired or no debounce)
    """
    if debounce_until is None:
        return 0

    now = datetime.now(UTC)
    if now >= debounce_until:
        return 0

    return int((debounce_until - now).total_seconds())


def should_process_feedback(response: ParsedFeedbackResponse) -> bool:
    """
    Check if feedback should be processed.

    Args:
        response: The parsed feedback response

    Returns:
        True if the submit checkbox is checked
    """
    return response.submitted


def start_feedback_debounce(
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS,
) -> datetime:
    """
    Calculate a new debounce expiration time.

    Args:
        debounce_seconds: Debounce period in seconds

    Returns:
        Datetime when the debounce expires
    """
    return datetime.now(UTC) + timedelta(seconds=debounce_seconds)


def update_feedback_with_countdown(
    original_comment: str,
    seconds_remaining: int,
) -> str:
    """
    Update a feedback comment to show the current countdown status.

    Args:
        original_comment: The original comment body
        seconds_remaining: Seconds remaining in countdown

    Returns:
        Updated comment body with countdown
    """
    new_notice = _generate_feedback_debounce_notice(seconds_remaining)

    # Pattern to match the debounce notice block
    debounce_pattern = re.compile(
        r"> \*\*Debounce active:\*\*.*?(?=\n\n---|\n---|\Z)",
        re.DOTALL,
    )

    processing_pattern = re.compile(r"> Processing feedback\.\.\.")

    # Try to replace existing notice
    if debounce_pattern.search(original_comment):
        return debounce_pattern.sub(new_notice, original_comment)
    elif processing_pattern.search(original_comment):
        return processing_pattern.sub(new_notice, original_comment)

    # If no existing notice, add before the final authored-by line
    authored_pattern = re.compile(r"\n---\n\n\*Authored-by: egg\*")
    match = authored_pattern.search(original_comment)
    if match:
        insert_pos = match.start()
        return (
            original_comment[:insert_pos]
            + "\n\n---\n\n"
            + new_notice
            + original_comment[insert_pos:]
        )

    # Fallback: append at end
    return original_comment.rstrip() + "\n\n---\n\n" + new_notice
