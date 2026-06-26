"""Overseer calibration corpus package (issue #2270, slice-1, AC-3).

The labelled known-normal / known-bad ``EventStreamSnapshot`` fixtures plus the
harness contract every later overseer detector (slices 4 / 7 / 8) is validated
against. See :mod:`overseer_calibration.corpus` for the data model and loader.
"""

from __future__ import annotations

from overseer_calibration.corpus import (
    DETECTOR_DELIVERY_SLICES,
    CorpusRow,
    Detector,
    EventStreamSnapshot,
    ExpectedFinding,
    Finding,
    FindingClass,
    Label,
    LifecycleOwner,
    RunningAgent,
    Scoreboard,
    Severity,
    assert_row,
    evaluate,
    load_corpus,
    match_finding,
    null_detector,
    register_detector,
    resolve_detector,
)

__all__ = [
    "DETECTOR_DELIVERY_SLICES",
    "CorpusRow",
    "Detector",
    "EventStreamSnapshot",
    "ExpectedFinding",
    "Finding",
    "FindingClass",
    "Label",
    "LifecycleOwner",
    "RunningAgent",
    "Scoreboard",
    "Severity",
    "assert_row",
    "evaluate",
    "load_corpus",
    "match_finding",
    "null_detector",
    "register_detector",
    "resolve_detector",
]
