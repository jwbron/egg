"""reviews helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim; patched/barrel-resident globals reached via _pkg so
patch("routes.pipelines.<name>") keeps intercepting.
"""

from __future__ import annotations

import json
from pathlib import Path

import routes.pipelines as _pkg  # noqa: E402,F401
from models import AggregatedReviewResult, ReviewVerdict

from ._drafts import _verdict_path_for_type


def _read_review_verdict(
    repo_path: Path,
    phase: str,
    reviewer_type: str = "code",
    pipeline_mode: str = "issue",
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> ReviewVerdict | None:
    """Read a typed review verdict JSON from the repo.

    Returns None if the file is missing or malformed (treated as approved
    for graceful degradation).
    """
    verdict_rel = _verdict_path_for_type(
        phase,
        reviewer_type,
        issue_number=issue_number,
        pipeline_id=pipeline_id,
    )
    verdict_file = repo_path / verdict_rel

    if not verdict_file.exists():
        _pkg.logger.warning(
            "Verdict file not found, treating as approved",
            path=str(verdict_file),
            reviewer_type=reviewer_type,
        )
        return None

    try:
        raw = verdict_file.read_text()
        data = json.loads(raw)
        return ReviewVerdict(**data)
    except (json.JSONDecodeError, Exception) as e:
        _pkg.logger.warning(
            "Failed to parse verdict file, treating as approved",
            path=str(verdict_file),
            reviewer_type=reviewer_type,
            error=str(e),
        )
        return None


def _read_tester_gaps(
    repo_path: Path,
    identifier: int | str | None = None,
) -> str | None:
    """Read tester output and extract gap findings for feedback to the coder.

    Reads `.egg-state/agent-outputs/{identifier}-tester-output.json` (with
    fallback to `tester-output.json`) and formats any test failures and gaps
    found into a summary string.

    Falls back to scanning the `summary` field for failure keywords when
    `gaps_found` is not present (backwards compat with old tester outputs).

    Args:
        repo_path: Path to the repository.
        identifier: Pipeline/issue identifier for namespaced filenames.

    Returns:
        Formatted gap summary string, or None if no gaps found.
    """
    outputs_dir = repo_path / ".egg-state" / "agent-outputs"

    # Try prefixed filename first, fall back to old global filename
    tester_output_file = None
    if identifier is not None:
        prefixed = outputs_dir / f"{identifier}-tester-output.json"
        if prefixed.exists():
            tester_output_file = prefixed
    if tester_output_file is None:
        tester_output_file = outputs_dir / "tester-output.json"

    if not tester_output_file.exists():
        return None

    try:
        raw = tester_output_file.read_text()
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        _pkg.logger.warning(
            "Failed to parse tester output file",
            path=str(tester_output_file),
            error=str(e),
        )
        return None

    if not isinstance(data, dict):
        return None

    sections: list[str] = []

    tests_failed = data.get("tests_failed", 0)
    if tests_failed:
        sections.append(f"- **{tests_failed}** test(s) failed")

    gaps_found = data.get("gaps_found")
    if gaps_found and isinstance(gaps_found, list):
        # Cap at 10 gaps to avoid prompt bloat
        capped = gaps_found[:10]
        for gap in capped:
            gap_str = str(gap)[:200]
            sections.append(f"- {gap_str}")
        if len(gaps_found) > 10:
            sections.append(f"- ... and {len(gaps_found) - 10} more gaps")
    elif not tests_failed:
        # Backwards compat: scan summary for failure keywords
        summary = data.get("summary", "")
        if isinstance(summary, str) and any(
            kw in summary.lower() for kw in ("fail", "gap", "missing", "error", "deficien")
        ):
            sections.append(f"- Tester summary: {summary}")

    if not sections:
        return None

    return f"{_pkg.TESTER_FINDINGS_HEADER}\n" + "\n".join(sections)


def _aggregate_review_verdicts(
    verdicts: dict[str, ReviewVerdict | None],
) -> AggregatedReviewResult:
    """Aggregate multiple typed review verdicts into an overall result.

    Returns:
        AggregatedReviewResult with:
        - verdict: "approved" or "needs_revision" (any needs_revision → overall needs_revision)
        - blocking_feedback: combined feedback from needs_revision verdicts only
        - advisory_content: analysis and suggestions from ALL verdicts (including approved)

        Missing/None verdicts are skipped.
    """
    overall = "approved"
    feedback_sections: list[str] = []
    advisory_sections: list[str] = []

    for reviewer_type, verdict in verdicts.items():
        if verdict is None:
            continue

        # Collect blocking feedback from needs_revision verdicts
        if verdict.verdict == "needs_revision":
            overall = "needs_revision"
            section = f"### {reviewer_type} reviewer\n"
            if verdict.feedback:
                section += verdict.feedback
            elif verdict.summary:
                section += verdict.summary
            feedback_sections.append(section)

        # Collect analysis and suggestions from ALL verdicts (including approved)
        advisory_parts: list[str] = []
        if verdict.analysis:
            advisory_parts.append(verdict.analysis)
        if verdict.suggestions:
            advisory_parts.append(f"**Suggestions:** {verdict.suggestions}")
        if advisory_parts:
            advisory_sections.append(
                f"### {reviewer_type} reviewer\n" + "\n\n".join(advisory_parts)
            )

    blocking_feedback = "\n\n".join(feedback_sections) if feedback_sections else ""
    advisory_content = "\n\n".join(advisory_sections) if advisory_sections else ""
    return AggregatedReviewResult(
        verdict=overall,
        blocking_feedback=blocking_feedback,
        advisory_content=advisory_content,
    )
