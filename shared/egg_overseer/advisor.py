"""Opus advisor wrapper for the overseer (issue #1962).

Implements the executor → advisor handoff under the
[advisor strategy](https://claude.com/blog/the-advisor-strategy):
Haiku 4.5 keeps driving the overseer cycle (max_turns=1); when it flags
an anomaly AND a Tier-1 health alert has tripped, this module is invoked
to consult Opus for a richer verdict (alert composition, priority, file
recommendation).

The wrapper is invoked from the sandbox-side CLI verb
``egg-orch overseer consult-advisor``
(``sandbox/egg_lib/orch_cli.py::cmd_overseer_consult_advisor``). The
underlying ``run_agent_async`` Opus call therefore runs sandbox-side and
stays on the LLM-execution side of the EGG200 boundary documented in
``docs/guides/agent-mode-design.md`` — the orchestrator pod never holds
Anthropic credentials.

Implementation choice: **Option B (two-call pattern)** per the SDK
spike recorded at ``.egg-state/agent-outputs/1962-sdk-spike.md``. The
vendored ``claude-agent-sdk==0.1.65`` does not expose the native
``advisor_20260301`` tool, so we issue a separate ``run_agent_async``
call against the configured Opus model with a single-turn prompt that
follows the ``decision-20`` opt-3 contract (distilled summary).

The function returns a structured ``AdvisorVerdict`` whose ``decision``
field drives one of three branches in the caller
(``sandbox/overseer_monitor.py``):

* ``"watch"`` — emit nothing this cycle.
* ``"alert"`` — emit ``OVERSEER_ALERT`` with ``alert_summary`` /
  ``alert_detail`` / translated priority.
* ``"file_issue"`` — emit ``OVERSEER_ALERT`` with
  ``recommendation=file_issue`` and a fully composed
  ``recommendation_payload``; the human gates filing via the existing
  HITL flow.

The advisor itself never calls ``gh issue create``; that runs
sandbox-side via ``egg-orch overseer file-issue`` after human approval.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, model_validator  # noqa: TC002

from egg_overseer.scrubbing import scrub_secrets

if TYPE_CHECKING:
    # Avoid hard-importing PipelineConfig here so the shared package
    # can be imported without the orchestrator package present (e.g.
    # in lightweight unit-test environments).
    from orchestrator.models import PipelineConfig

logger = logging.getLogger(__name__)


_PROMPT_SYSTEM = (
    "You are the overseer's advisor. Haiku flagged an anomaly and a "
    "Tier-1 health alert is active. Decide whether to: (a) watch (no "
    "action), (b) alert the human via OVERSEER_ALERT, or (c) recommend "
    "filing a GitHub issue. Return a JSON object matching the "
    "AdvisorVerdict schema. Do NOT include any other text."
)


# Default byte cap for the ``recent_log_lines`` block (issue #2120). The
# advisor model is opus-class with ~200k token context; 256 KiB sits
# well under that with comfortable headroom for the system prompt,
# classification, health alerts, progress events, and instructions.
# Tunable per-pipeline via ``PipelineConfig.overseer_advisor_recent_log_bytes_cap``.
_DEFAULT_RECENT_LOG_BYTES_CAP = 256_000


class AdvisorVerdict(BaseModel):
    """Structured verdict returned by ``consult_advisor``."""

    decision: Literal["alert", "file_issue", "watch"]
    priority: Literal["p0", "p1", "p2", "p3"] | None = None
    alert_summary: str | None = None
    alert_detail: str | None = None
    issue_title: str | None = None  # required when decision=="file_issue"
    issue_body: str | None = None  # required when decision=="file_issue"
    reasoning: str

    @model_validator(mode="after")
    def _check_file_issue_payload(self) -> AdvisorVerdict:
        if self.decision == "file_issue":
            if not self.issue_title or not self.issue_body:
                raise ValueError(
                    "AdvisorVerdict: decision='file_issue' requires both issue_title and issue_body"
                )
            if not self.priority:
                raise ValueError("AdvisorVerdict: decision='file_issue' requires a priority")
        if self.decision == "alert":
            if not self.alert_summary:
                raise ValueError("AdvisorVerdict: decision='alert' requires alert_summary")
            # The OVERSEER_ALERT consumer translates priority via
            # ``egg_overseer.priority.label_to_alert``; a missing
            # priority crashes that helper with "unrecognised label".
            # Catch the gap at parse time so we surface a clearer
            # error than a downstream KeyError.
            if not self.priority:
                raise ValueError("AdvisorVerdict: decision='alert' requires a priority")
        return self


class AdvisorParseError(RuntimeError):
    """Raised when the advisor returns text that doesn't parse to AdvisorVerdict."""


def _truncate_log_lines_by_bytes(
    lines: list[str],
    cap_bytes: int,
) -> tuple[list[str], int, int]:
    """Drop oldest lines until the joined block fits under ``cap_bytes``.

    Walks from the most-recent line backward so the tail (highest-signal
    for the advisor) is preserved. Byte accounting matches the join
    used in :func:`_build_prompt`: each line's UTF-8 length plus one
    byte for the newline that joins it to its neighbor.

    A single line larger than ``cap_bytes`` is dropped along with
    everything before it; the prompt section will fall through to
    "(none)" and the caller logs the truncation event so pathological
    producers stay observable.

    Returns ``(kept_lines, dropped_line_count, dropped_byte_count)``.
    ``cap_bytes <= 0`` disables the cap and returns the input unchanged.
    """
    if cap_bytes <= 0 or not lines:
        return list(lines), 0, 0
    kept_reversed: list[str] = []
    total_bytes = 0
    for line in reversed(lines):
        size = len(line.encode("utf-8")) + 1  # +1 == joining "\n"
        if total_bytes + size > cap_bytes:
            break
        kept_reversed.append(line)
        total_bytes += size
    kept = list(reversed(kept_reversed))
    dropped_count = len(lines) - len(kept)
    if dropped_count == 0:
        return kept, 0, 0
    dropped_bytes = sum(len(line.encode("utf-8")) + 1 for line in lines[:dropped_count])
    return kept, dropped_count, dropped_bytes


def _build_prompt(
    *,
    classification: dict[str, Any],
    health_alerts: list[dict[str, Any]],
    progress_events: list[dict[str, Any]],
    recent_log_lines: list[str],
    truncation_marker: str | None = None,
) -> str:
    """Build the single-turn user prompt per ``decision-20`` opt-3.

    Distilled summary: classification + last N progress events + active
    health alerts + last K log lines.

    ``truncation_marker``, when non-None, is rendered above the log
    block so the advisor knows the prompt-builder dropped earlier
    lines to fit the byte cap (issue #2120).
    """
    sections: list[str] = []
    sections.append("## Haiku classification")
    sections.append(json.dumps(classification, indent=2, sort_keys=True))
    sections.append("")
    sections.append("## Active Tier-1 health alerts")
    if health_alerts:
        sections.append(json.dumps(health_alerts, indent=2, sort_keys=True))
    else:
        sections.append("(none)")
    sections.append("")
    sections.append("## Recent progress events (last N)")
    if progress_events:
        sections.append(json.dumps(progress_events, indent=2, sort_keys=True))
    else:
        sections.append("(none)")
    sections.append("")
    sections.append("## Recent container log lines (last K)")
    if truncation_marker is not None:
        sections.append(truncation_marker)
    if recent_log_lines:
        sections.append("\n".join(recent_log_lines))
    elif truncation_marker is None:
        sections.append("(none)")
    sections.append("")
    sections.append(
        "Return ONLY a JSON object matching the AdvisorVerdict schema. "
        "Fields: decision (alert|file_issue|watch), priority "
        "(p0|p1|p2|p3 — required when decision=file_issue or alert), "
        "alert_summary, alert_detail, issue_title, issue_body, reasoning."
    )
    return "\n".join(sections)


async def consult_advisor(
    *,
    classification: dict[str, Any],
    health_alerts: list[dict[str, Any]],
    progress_events: list[dict[str, Any]],
    recent_log_lines: list[str],
    config: PipelineConfig | None = None,
    recent_log_bytes_cap: int | None = None,
    _agent_runner: Any = None,
) -> AdvisorVerdict:
    """Issue the advisor call and return the structured verdict.

    Args:
        classification: Haiku's classification output (anomaly type,
            confidence, reasoning).
        health_alerts: Active Tier-1 health alerts at advise time.
        progress_events: Last N progress events from the affected agent.
        recent_log_lines: Last K container log lines.
        config: ``PipelineConfig`` with at least ``overseer_advisor_model``
            populated. Defaults to ``opus`` when None.
        recent_log_bytes_cap: Optional byte cap for the
            ``recent_log_lines`` prompt block (issue #2120). Resolution
            order: explicit arg → ``config.overseer_advisor_recent_log_bytes_cap``
            → ``_DEFAULT_RECENT_LOG_BYTES_CAP``. ``0`` disables the cap.
        _agent_runner: Test seam — pass an awaitable callable
            ``(prompt: str, model: str) -> str`` to override the default
            ``run_agent_async`` invocation. Production callers leave
            this None.

    Returns:
        Validated ``AdvisorVerdict``. When ``decision='file_issue'``,
        ``issue_body`` has already been run through ``scrub_secrets``.

    Raises:
        AdvisorParseError: if the SDK response does not parse to a
            valid AdvisorVerdict.
    """
    model = config.overseer_advisor_model if config is not None else "opus"
    if recent_log_bytes_cap is None:
        recent_log_bytes_cap = (
            getattr(config, "overseer_advisor_recent_log_bytes_cap", None)
            if config is not None
            else None
        )
    if recent_log_bytes_cap is None:
        recent_log_bytes_cap = _DEFAULT_RECENT_LOG_BYTES_CAP

    kept_lines, dropped_lines, dropped_bytes = _truncate_log_lines_by_bytes(
        recent_log_lines, recent_log_bytes_cap
    )
    truncation_marker: str | None = None
    if dropped_lines > 0:
        truncation_marker = (
            f"[... {dropped_lines} earlier line(s) dropped (~{dropped_bytes} bytes) "
            f"to fit recent_log_lines byte cap ({recent_log_bytes_cap} bytes); "
            "most-recent lines retained ...]"
        )
        # Metric: pathological log producers show up as a stream of
        # ``advisor_log_truncated`` events. Emitted before the SDK call
        # so even an SDK failure leaves the truncation observable.
        logger.info(
            "overseer_event",
            extra={
                "event": "advisor_log_truncated",
                "dropped_lines": dropped_lines,
                "dropped_bytes": dropped_bytes,
                "cap_bytes": recent_log_bytes_cap,
                "input_line_count": len(recent_log_lines),
                "model": model,
            },
        )

    prompt = _build_prompt(
        classification=classification,
        health_alerts=health_alerts,
        progress_events=progress_events,
        recent_log_lines=kept_lines,
        truncation_marker=truncation_marker,
    )

    if _agent_runner is None:
        # Lazily import the SDK runner so unit tests that mock
        # ``_agent_runner`` do not need the SDK on the path.
        try:
            from egg_agent.client import run_agent_async
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "consult_advisor: egg_agent.client.run_agent_async is "
                "not importable; pass _agent_runner for tests"
            ) from exc

        async def _default_runner(p: str, m: str) -> str:
            # SDK kwarg is `system_prompt`, not `system`. Returning
            # `result.stdout` gives the assistant's text body for the
            # single-turn call; `str(result)` would give the
            # AgentResult repr and fail JSON parsing downstream.
            # Optional code-fence stripping happens after the runner
            # returns (handles both this runner and test runners).
            result = await run_agent_async(
                prompt=p,
                model=m,
                system_prompt=_PROMPT_SYSTEM,
                max_turns=1,
            )
            stdout_attr = getattr(result, "stdout", None)
            return stdout_attr if isinstance(stdout_attr, str) else str(result)

        runner = _default_runner
    else:
        runner = _agent_runner

    raw = await runner(prompt, model)

    # Strip optional ``` ```json fences the model occasionally wraps
    # the JSON in (defensive — the prompt instructs against fences but
    # real outputs still slip them in). Applied here so it covers both
    # the default runner and caller-supplied test runners.
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall-back: pull the first balanced JSON object out of prose.
        # Under ``max_turns=1`` Claude often emits ``Here's the
        # verdict:\n{...}`` instead of pure JSON; ``raw_decode`` is
        # string-aware so braces inside JSON string values don't fool
        # the scan. Iterate over candidate ``{`` positions so a stray
        # brace in leading prose (e.g. ``see {field} below: {...}``)
        # doesn't lock us onto an unparseable snippet.
        decoder = json.JSONDecoder()
        payload = None
        last_exc: json.JSONDecodeError | None = None
        pos = 0
        while True:
            start = cleaned.find("{", pos)
            if start == -1:
                break
            try:
                payload, _ = decoder.raw_decode(cleaned[start:])
                break
            except json.JSONDecodeError as exc:
                last_exc = exc
                pos = start + 1
        if payload is None:
            # No `{` ever found → no useful inner exception; otherwise
            # chain the most recent ``raw_decode`` failure for context.
            # Scrub before raising: ``raw`` is the unparsed model output
            # and ends up in stderr via ``cmd_overseer_consult_advisor``.
            # NOTE: ``__cause__`` (last_exc) preserves the original
            # ``JSONDecodeError`` whose ``str()`` can echo input values.
            # Safe today because no caller renders the chained traceback
            # — if you add ``traceback.format_exc()`` or
            # ``logger.exception()`` upstream, scrub there too.
            raise AdvisorParseError(
                scrub_secrets(f"consult_advisor: SDK response is not valid JSON: {raw!r}")
            ) from last_exc

    try:
        verdict = AdvisorVerdict.model_validate(payload)
    except Exception as exc:
        # Scrub: ``payload`` and the pydantic error both echo input
        # values that may include credentials the model parroted back.
        # Same ``__cause__`` caveat as the JSON-decode path above:
        # ``ValidationError`` carries unscrubbed input; safe only while
        # callers don't render the chained traceback.
        raise AdvisorParseError(
            scrub_secrets(
                f"consult_advisor: SDK response failed AdvisorVerdict "
                f"validation: {exc}; payload={payload!r}"
            )
        ) from exc

    # Defense-in-depth: scrub the body before it leaves this function.
    if verdict.decision == "file_issue" and verdict.issue_body:
        verdict.issue_body = scrub_secrets(verdict.issue_body)

    logger.info(
        "overseer_event",
        extra={
            "event": "advisor_invoked",
            "decision": verdict.decision,
            "priority": verdict.priority,
            "reasoning": verdict.reasoning,
            "model": model,
        },
    )
    return verdict


__all__ = [
    "AdvisorVerdict",
    "AdvisorParseError",
    "consult_advisor",
]
