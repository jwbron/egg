"""Deterministic loop detector for the #2270 detection plane (#3665 slice-3).

Implements the empirical finding from issue #3665:

> counting *tool inputs never issued before in the session* over a trailing
> window separates a loop from work cleanly: a working agent produces new
> ones and a loop of any length produces none.

The detector reads ``midturn_messages`` from the :class:`EventStreamSnapshot`
(populated by slice-1, TASK-1-1) and tracks the set of tool-input hashes seen
across evaluations. If a trailing window of polls produces zero *new* tool
inputs, the detector fires ``tool_input_loop`` / ``high``.

Key design decisions (from the plan's HITL resolution):

* **Not keyed on cycle shape.** Observed loops had 1-, 2-, 3-, and 8-cycles,
  plus a variant with 64 distinct inputs at 80% dominance and a 3-cycle at
  22% dominance. Any rule keyed on shape, dominance, or distinct-count
  misses most of them.
* **Hash the full ``(tool_name, input)`` pair.** Truncating the input at any
  length reintroduces the prefix-collapse that TASK-3-2 exists to remove.
* **Zero new inputs over a trailing window** — every observed instance scored
  exactly zero; healthy agents scored well above it.

The detector is stateful: it maintains a rolling history of input-hash sets
across evaluations. The state is keyed by ``pipeline_id`` so multiple
pipelines can be tracked independently.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from health_checks.types import Finding, Severity

# Finding-class string. Emitted as a plain string; the detection plane matches
# structurally on the raw string, so this need not be in the FindingClass enum.
FINDING_TOOL_INPUT_LOOP = "tool_input_loop"

# Default window size: number of consecutive polls with zero new inputs
# before firing. The issue observed loops persisting for minutes, so a
# 3-poll window at the 5s RUNTIME_TICK cadence = 15s of silence.
_DEFAULT_ZERO_NEW_INPUT_WINDOW = 3

# Default maximum history to retain per pipeline (bounded to prevent
# unbounded growth in long-running pipelines).
_DEFAULT_HISTORY_MAX = 100


def _midturn_messages(snapshot: Any) -> tuple[dict[str, Any], ...]:
    """Extract midturn_messages from the snapshot, defensively."""
    raw = getattr(snapshot, "midturn_messages", None)
    if not isinstance(raw, (tuple, list)):
        return ()
    return tuple(m for m in raw if isinstance(m, dict))


def _extract_input_hashes(messages: tuple[dict[str, Any], ...]) -> set[str]:
    """Extract the set of distinct input_hash values from midturn_messages."""
    hashes: set[str] = set()
    for msg in messages:
        h = msg.get("input_hash")
        if h:
            hashes.add(str(h))
    return hashes


class ToolInputLoopTracker:
    """Stateful tracker for the zero-new-input window per pipeline.

    Maintains a rolling history of input-hash sets across evaluations.
    Each call to :meth:`observe` records the set of hashes seen in the
    current poll and returns whether the trailing window is all-zero.
    """

    def __init__(
        self,
        *,
        window: int = _DEFAULT_ZERO_NEW_INPUT_WINDOW,
        history_max: int = _DEFAULT_HISTORY_MAX,
    ) -> None:
        self._window = window
        self._history_max = history_max
        # pipeline_id -> deque of (timestamp, set_of_new_hashes)
        self._per_pipeline: dict[str, deque[tuple[float, set[str]]]] = {}
        # pipeline_id -> set of all hashes ever seen
        self._seen: dict[str, set[str]] = {}

    def observe(
        self, pipeline_id: str, current_hashes: set[str]
    ) -> tuple[bool, int]:
        """Record the current poll's hashes and check the zero-new-input window.

        Returns ``(is_looping, zero_count)`` where ``is_looping`` is True when
        the trailing ``window`` polls have all produced zero new inputs, and
        ``zero_count`` is the current consecutive zero-new-input count.
        """
        seen = self._seen.setdefault(pipeline_id, set())
        new_hashes = current_hashes - seen
        seen.update(current_hashes)

        history = self._per_pipeline.setdefault(pipeline_id, deque(maxlen=self._history_max))
        history.append((time.monotonic(), new_hashes))

        # Count consecutive trailing polls with zero new inputs.
        zero_count = 0
        for _ts, hashes in reversed(history):
            if not hashes:
                zero_count += 1
            else:
                break

        is_looping = zero_count >= self._window
        return is_looping, zero_count

    def reset(self, pipeline_id: str | None = None) -> None:
        """Clear tracking state for one pipeline or all."""
        if pipeline_id is None:
            self._per_pipeline.clear()
            self._seen.clear()
        else:
            self._per_pipeline.pop(pipeline_id, None)
            self._seen.pop(pipeline_id, None)


# Module-level singleton tracker. The DetectionPlane holds one instance
# and passes it to detect_tool_input_loop on each evaluation.
_default_tracker: ToolInputLoopTracker | None = None


def get_default_loop_tracker() -> ToolInputLoopTracker:
    """Return the process-wide loop tracker singleton."""
    global _default_tracker
    if _default_tracker is None:
        _default_tracker = ToolInputLoopTracker()
    return _default_tracker


def reset_default_loop_tracker() -> None:
    """Reset the process-wide loop tracker (tests)."""
    global _default_tracker
    _default_tracker = None


def detect_tool_input_loop(
    snapshot: Any,
    *,
    tracker: ToolInputLoopTracker | None = None,
    window: int = _DEFAULT_ZERO_NEW_INPUT_WINDOW,
) -> Finding | None:
    """Fire when an agent produces zero new tool inputs over a trailing window.

    The empirical finding from issue #3665: across every observed repetition
    loop — single-input, 2-, 3-, and 8-cycles — counting *tool inputs never
    issued before in the session* over a trailing window separates a loop
    from work cleanly. A working agent produces new ones; a loop of any
    length produces none.

    This detector is **not** keyed on cycle shape, dominance, or distinct-count
    — those miss most observed instances. It fires only when the trailing
    ``window`` polls have all produced zero new tool-input hashes.

    Deterministic → ``requires_adjudication=False``.
    """
    # Only fire when the phase is RUNNING — a parked phase is not a loop.
    phase_state = getattr(snapshot, "phase_state", {}) or {}
    if not isinstance(phase_state, dict):
        phase_state = {}
    if str(phase_state.get("status", "")).upper() != "RUNNING":
        return None

    messages = _midturn_messages(snapshot)
    if not messages:
        return None

    current_hashes = _extract_input_hashes(messages)
    if not current_hashes:
        return None

    pipeline_id = getattr(snapshot, "pipeline_id", "")
    if not pipeline_id:
        return None

    tracker = tracker or get_default_loop_tracker()
    is_looping, zero_count = tracker.observe(pipeline_id, current_hashes)

    if not is_looping:
        return None

    # Build evidence: the tool input hash and window size.
    # Use the most recent message for context.
    last_msg = messages[-1] if messages else {}
    evidence = {
        "pipeline_id": pipeline_id,
        "phase": getattr(snapshot, "phase", ""),
        "zero_new_input_polls": zero_count,
        "window_size": window,
        "last_tool_name": last_msg.get("tool_name", ""),
        "last_input_hash": last_msg.get("input_hash", ""),
        "total_distinct_inputs_seen": len(tracker._seen.get(pipeline_id, set())),
    }

    return Finding(
        finding_class=FINDING_TOOL_INPUT_LOOP,
        severity=Severity.HIGH,
        evidence=evidence,
        recommended_action=(
            "Agent has produced zero new tool inputs for "
            f"{zero_count} consecutive polls (window={window}). "
            "This is a repetition loop — the same tool call(s) are being "
            "re-issued without progress. Nudge or respawn the agent."
        ),
        requires_adjudication=False,
        detector_key="tool_input_loop",
    )


detect_tool_input_loop.detector_key = "tool_input_loop"  # type: ignore[attr-defined]
detect_tool_input_loop.name = "tool_input_loop_detector"  # type: ignore[attr-defined]


__all__ = [
    "FINDING_TOOL_INPUT_LOOP",
    "ToolInputLoopTracker",
    "detect_tool_input_loop",
    "get_default_loop_tracker",
    "reset_default_loop_tracker",
]
