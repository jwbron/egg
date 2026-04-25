"""Priority-dimension mapping for overseer alerts and filed issues.

Three consumers in the codebase use priority labels with different
vocabularies:

(1) ``egg-orch overseer alert --priority`` — ``low|medium|high``
    (sandbox/egg_lib/orch_cli.py).
(2) ``egg-orch overseer file-issue --priority`` — ``p0|p1|p2|p3``
    (matches the GitHub label convention).
(3) Advisor verdict (``shared/egg_overseer/advisor.py``) — ``p0|p1|p2|p3``
    (uses the file-issue dimension natively).

This module ships the canonical mapping between the two vocabularies so
any consumer can translate without re-implementing the logic.

Mapping rules (per TASK-1-2 in the #1962 plan):

* ``low``  ↔ ``p3``
* ``medium`` ↔ ``p2``
* ``high`` ↔ ``p1``
* ``p0`` is reserved for opt-in human escalation; the advisor never
  produces it automatically. ``label_to_alert("p0")`` therefore returns
  ``"high"`` so legacy alert callers can still surface a p0 escalation
  without inventing a new alert tier.
"""

from __future__ import annotations

from typing import Literal

AlertPriority = Literal["low", "medium", "high"]
LabelPriority = Literal["p0", "p1", "p2", "p3"]


_ALERT_TO_LABEL: dict[str, str] = {
    "low": "p3",
    "medium": "p2",
    "high": "p1",
}

_LABEL_TO_ALERT: dict[str, str] = {
    "p0": "high",  # opt-in human escalation; never produced by advisor.
    "p1": "high",
    "p2": "medium",
    "p3": "low",
}


def alert_to_label(priority: AlertPriority) -> LabelPriority:
    """Translate an ``egg-orch overseer alert`` priority to a GitHub label.

    Args:
        priority: ``"low" | "medium" | "high"`` alert priority.

    Returns:
        Matching ``"p1" | "p2" | "p3"`` GitHub label. ``p0`` is never
        produced by this direction (no alert tier maps to it).

    Raises:
        ValueError: if ``priority`` is not one of the recognised values.
    """
    label = _ALERT_TO_LABEL.get(priority)
    if label is None:
        raise ValueError(
            f"alert_to_label: unrecognised alert priority {priority!r}; "
            f"expected one of {sorted(_ALERT_TO_LABEL)}"
        )
    return label  # type: ignore[return-value]


def label_to_alert(priority: LabelPriority) -> AlertPriority:
    """Translate a ``p0..p3`` GitHub label to an alert priority.

    Args:
        priority: ``"p0" | "p1" | "p2" | "p3"`` GitHub label priority.

    Returns:
        Matching ``"low" | "medium" | "high"`` alert priority. ``p0``
        collapses to ``"high"`` because the alert dimension does not
        have a dedicated p0 tier.

    Raises:
        ValueError: if ``priority`` is not one of the recognised values.
    """
    alert = _LABEL_TO_ALERT.get(priority)
    if alert is None:
        raise ValueError(
            f"label_to_alert: unrecognised label priority {priority!r}; "
            f"expected one of {sorted(_LABEL_TO_ALERT)}"
        )
    return alert  # type: ignore[return-value]


__all__ = [
    "AlertPriority",
    "LabelPriority",
    "alert_to_label",
    "label_to_alert",
]
