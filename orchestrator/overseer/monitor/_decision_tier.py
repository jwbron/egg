"""OverseerMonitor classifier / decision-tier accessor methods (#3312, slice-8).

Method bodies extracted verbatim from the pre-split ``overseer/monitor.py``;
bound onto ``OverseerMonitor`` in the barrel. ``_accepts_kwarg`` is reached
through the package barrel (``_pkg``) so it stays a single definition.
"""

from __future__ import annotations

import overseer.monitor as _pkg
from agent_model_resolution import resolve_overseer_model
from overseer.classifier import check_decision_consistency, classify_stall
from overseer.decision_maker import decide_corrective_action, decide_escalation_level


async def _classify_stall(
    self,
    logs: list[dict],
    progress: list[dict],
    consensus: dict | None = None,
    container_logs: str | None = None,
) -> dict:
    if self._classifier and hasattr(self._classifier, "classify_stall"):
        return await self._classifier.classify_stall(
            logs, progress, consensus=consensus, container_logs=container_logs
        )
    return await classify_stall(logs, progress, consensus=consensus, container_logs=container_logs)


async def _check_decision_consistency_cls(
    self, phase_output: dict, prior_decisions: list[dict]
) -> dict:
    if self._classifier and hasattr(self._classifier, "check_decision_consistency"):
        return await self._classifier.check_decision_consistency(phase_output, prior_decisions)
    return await check_decision_consistency(phase_output, prior_decisions)


def _resolve_tier_model(self, tier: str) -> str:
    """Resolve an overseer decision *tier* (classify/routine/adversarial)
    to a model alias via :func:`resolve_overseer_model` (#2270 slice-9).

    Single source of model plumbing so the deprecated
    ``overseer_decision_maker_model`` field is inert at this layer.
    """
    return resolve_overseer_model(tier, self.config).claude_code_alias


async def _decide_corrective_action(
    self,
    classification: dict,
    context: dict,
    *,
    redirect_history: list[dict] | None = None,
) -> dict:
    model = self._resolve_tier_model("routine")
    if self._decision_maker and hasattr(self._decision_maker, "decide_corrective_action"):
        method = self._decision_maker.decide_corrective_action
        if _pkg._accepts_kwarg(method, "redirect_history"):
            return await method(
                classification,
                context,
                model=model,
                redirect_history=redirect_history,
            )
        # Test doubles with explicit signatures that pre-date the
        # redirect_history kwarg fall through here; the guard
        # downstream (_enforce_no_first_stall_restart) is bypassed
        # in that path, which is fine for tests that don't exercise
        # it.
        return await method(classification, context, model=model)
    return await decide_corrective_action(
        classification,
        context,
        model=model,
        redirect_history=redirect_history,
    )


async def _decide_escalation_level(
    self, classification: dict, redirect_history: list[dict], context: dict | None = None
) -> dict:
    model = self._resolve_tier_model("routine")
    if self._decision_maker and hasattr(self._decision_maker, "decide_escalation_level"):
        return await self._decision_maker.decide_escalation_level(
            classification, redirect_history, context=context, model=model
        )
    return await decide_escalation_level(
        classification, redirect_history, context=context, model=model
    )
