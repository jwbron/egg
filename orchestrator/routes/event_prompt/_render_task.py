"""Task-framing sections — contract task_description, operator kickback, issue anchor.

Renderers for the operator-directive sections pushed into every one-shot
prompt: :func:`_render_task_section` (#3123 contract ``task_description``),
:func:`_render_iteration_feedback_section` (#3231 per-iteration operator
kickback) with its :func:`_directive_meta_tag` helper, and
:func:`_issue_anchor_fallback` (#3163 synthesized anchor for pre-#3163
GitHub-issue contracts). AST-identical to the pre-split definitions —
pure refactor (#3312 slice-6).
"""

from __future__ import annotations

from typing import Any

from ._caps import (
    _ITERATION_FEEDBACK_TRUNCATION_SENTINEL,
    _TASK_TRUNCATION_SENTINEL,
    ITERATION_FEEDBACK_MAX_CHARS,
    TASK_DESCRIPTION_MAX_CHARS,
)


def _render_task_section(task_description: str) -> str:
    """Render the contract's ``task_description`` as a pushed section (#3123).

    The #3033/#3042 channel made the submit description reliably land in
    ``contract.task_description``, but delivery stayed pull-based: nothing
    in the per-event prompt or the role-scoped task views surfaced it, so
    an agent could complete a whole slice without ever reading the
    operator's directives (observed live: a slice coder reimplemented 12
    completed tasks from scratch past a prominent "ADOPT, DO NOT
    REIMPLEMENT" directive). This section closes the last hop by pushing
    the text into every one-shot invocation.

    Truncated at ``TASK_DESCRIPTION_MAX_CHARS`` with an explicit sentinel
    — the full text is one ``mcp__sdlc__show_contract`` call away.
    """
    if not (task_description or "").strip():
        return ""
    body = (task_description or "").strip()
    if len(body) > TASK_DESCRIPTION_MAX_CHARS:
        body = body[:TASK_DESCRIPTION_MAX_CHARS] + _TASK_TRUNCATION_SENTINEL
    return "\n".join(
        [
            "## Task & operator directives (contract ``task_description``)",
            "",
            "This is the operator's authoritative, submit-time task "
            "statement for the whole pipeline. It is BINDING for every "
            "event you handle: re-read it before structural decisions "
            "(what to adopt vs. implement from scratch, scope "
            "boundaries, hard requirements). If it conflicts with what "
            "you were about to do, the directive wins — course-correct "
            "or raise a HITL decision rather than proceeding.",
            "",
            body,
            "",
        ]
    )


def _directive_meta_tag(directive: dict[str, Any]) -> str:
    """Format a directive's iteration + timestamp as a parenthetical tag.

    Surfaces both the ``iteration_n`` ordering signal and the
    ``created_at`` wall-clock timestamp the route collects (#3231
    re-review note 1 — the timestamp was packed into the payload but
    never rendered). Returns ``""`` when neither is present so callers
    can append unconditionally without a dangling ``()``.
    """
    parts: list[str] = []
    it = directive.get("iteration_n")
    if it is not None:
        parts.append(f"iteration {it}")
    created = str(directive.get("created_at") or "").strip()
    if created:
        parts.append(created)
    return f" ({', '.join(parts)})" if parts else ""


def _render_iteration_feedback_section(iteration_feedback: dict[str, Any] | None) -> str:
    """Render the per-iteration operator kickback as a pushed section (#3231).

    The re-spawned producer's prompt is composed here. Without this
    section the producer re-reads its own prior on-disk draft and
    re-proposes it byte-for-byte — the operator's ``request_changes`` /
    ``change_approach`` silently no-ops (the #1283 / #1915 fake-cycle
    class, regressed for the orchestrator-owned event loop).

    The orchestrator's ``next-action`` route attaches the current phase
    execution's ``operator_directives`` (chronological) + — for the
    producer ``propose`` arm — the latest ``iteration_history`` summary
    onto the event_payload as a serializable ``iteration_feedback`` dict;
    this renderer turns it into the markdown the agent sees. The
    ``audience`` key (``"producer"`` / ``"reviewer"``) selects the
    framing: the producer is told to address-or-rebut every directive
    before re-proposing (an unchanged re-propose is a defect); the
    reviewer (re-reviewing the producer's directive-driven change, #2795)
    is told to evaluate the draft *against* the directive rather than
    NACK it back toward the pre-directive default rubric. Only the most
    recent directive is rendered in full; earlier directives are
    summarised one line each so the precedence chain is visible without
    re-reading the whole history.

    Truncated at ``ITERATION_FEEDBACK_MAX_CHARS`` with an explicit
    sentinel — the full directive history lives on
    ``PhaseExecution.operator_directives`` (one ``egg-orch brc get-state``
    call away). Returns ``""`` when the block carries no directives and
    no prior-iteration summary so the caller can omit the section
    entirely (golden-stable for the no-kickback path).
    """
    if not isinstance(iteration_feedback, dict):
        return ""
    directives = iteration_feedback.get("directives") or []
    prior_iteration = iteration_feedback.get("prior_iteration")
    if not directives and not prior_iteration:
        return ""
    for_reviewer = iteration_feedback.get("audience") == "reviewer"

    if for_reviewer:
        title = "## Operator feedback steering this phase — evaluate the draft against it"
    else:
        title = "## Operator feedback on the prior draft — address before re-proposing"
    lines: list[str] = [title, ""]

    # Frame the intro by audience, and only assert directive authority
    # when a directive is actually present (#3231 review item 4 — the
    # renderer also fires with a prior-iteration summary and no
    # directive, e.g. the legacy ``hitl_feedback`` migration path).
    if directives and for_reviewer:
        lines.append(
            "The operator kicked this phase back through a HITL phase gate; "
            "the directive(s) below are the operator's authoritative "
            "steering, and the producer's current draft is their response "
            "to it. Evaluate the draft AGAINST the directive: a faithful "
            "implementation of the operator's instruction is not grounds "
            "for a NACK even where it departs from the default rubric. Do "
            "NOT NACK the change back toward the pre-directive state — that "
            "fights the operator's steering and re-stalls the cycle."
        )
        lines.append("")
    elif directives:
        lines.append(
            "The operator kicked this phase back through a HITL phase gate. "
            "The directive(s) below are the operator's authoritative feedback "
            "on your prior proposal; they OVERRIDE prompt-template defaults "
            "and the contract's submit-time task framing where they conflict. "
            "You MUST address (or explicitly rebut) every point before "
            "re-proposing. **An unchanged re-propose after this feedback is a "
            "defect, not a valid cycle** — re-reading your own prior draft and "
            "re-proposing it verbatim will re-trip the gate with "
            "``content_changed: false``."
        )
        lines.append("")
    else:
        # Prior-iteration summary only (no directive to assert authority
        # over) — frame the summary without dangling directive prose.
        lines.append(
            "The prior iteration's BRC outcome is summarised below. Address "
            "what tripped the rubric before re-proposing. **An unchanged "
            "re-propose is a defect, not a valid cycle.**"
        )
        lines.append("")

    if isinstance(directives, list) and directives:
        # Render the most recent directive in full; earlier ones one line
        # each so the precedence chain is visible without re-bloating the
        # prompt. The orchestrator emits directives oldest→newest.
        latest = directives[-1] if isinstance(directives[-1], dict) else {}
        earlier = directives[:-1]
        if earlier:
            lines.append("### Earlier directives (chronological, for precedence)")
            lines.append("")
            for idx, d in enumerate(earlier, start=1):
                if not isinstance(d, dict):
                    continue
                text = str(d.get("feedback_text") or "").strip().replace("\n", " ")
                tag = _directive_meta_tag(d)
                if text:
                    lines.append(f"{idx}. {text}{tag}")
                else:
                    lines.append(f"{idx}. (no text recorded){tag}")
            lines.append("")

        meta = _directive_meta_tag(latest)
        header = "### Most recent directive"
        if meta:
            header += f"{meta} — address THIS round"
        lines.append(header)
        lines.append("")
        latest_text = str(latest.get("feedback_text") or "").strip()
        if latest_text:
            lines.append(latest_text)
        else:
            lines.append("(no text recorded)")
        lines.append("")

    if isinstance(prior_iteration, dict) and prior_iteration:
        lines.append("### Prior iteration summary")
        lines.append("")
        it_n = prior_iteration.get("iteration_n")
        if it_n is not None:
            lines.append(f"Frozen snapshot of iteration {it_n}'s BRC outcome:")
            lines.append("")
        verdict_matrix = prior_iteration.get("verdict_matrix") or {}
        if isinstance(verdict_matrix, dict) and verdict_matrix:
            verdicts = "; ".join(
                f"{edge}: {state}" for edge, state in sorted(verdict_matrix.items())
            )
            lines.append(f"- Verdict matrix: {verdicts}")
        nack_reasons = prior_iteration.get("nack_reasons") or []
        if isinstance(nack_reasons, list) and nack_reasons:
            lines.append(f"- NACK reasons ({len(nack_reasons)}):")
            for reason in nack_reasons:
                lines.append(f"  - {reason}")
        # Surface the prior iteration's final proposal commit(s) for parity
        # with the in-pod renderer (#3231 review item 2) — the producer can
        # diff against this SHA to see exactly what it last proposed.
        final_commits = prior_iteration.get("final_proposal_commit") or {}
        if isinstance(final_commits, dict) and final_commits:
            commits = "; ".join(
                f"{producer}: {sha}" for producer, sha in sorted(final_commits.items())
            )
            lines.append(f"- Final proposal commit(s): {commits}")
        if not verdict_matrix and not nack_reasons and not final_commits:
            lines.append("- (no verdict/NACK detail recorded for the prior iteration)")
        lines.append("")

    rendered = "\n".join(lines)
    if len(rendered) > ITERATION_FEEDBACK_MAX_CHARS:
        rendered = rendered[:ITERATION_FEEDBACK_MAX_CHARS] + _ITERATION_FEEDBACK_TRUNCATION_SENTINEL
    return rendered


def _issue_anchor_fallback(contract_data: dict[str, Any]) -> str:
    """Build a minimal task anchor from the contract's ``issue`` info (#3163).

    Contracts written before #3163 carry ``task_description: null`` on
    GitHub-issue pipelines (the #3042 exclusion), which left the binding
    task section silently absent for the most common pipeline type —
    observed live as a refiner adopting the *previous* pipeline's stale
    draft as its task. New contracts get a composed statement at
    creation; this fallback covers the contracts already committed to
    live branches, which no creation-time fix reaches.

    Keep the wording in sync with
    :func:`egg_contracts.loader.compose_task_description`'s GitHub-issue
    branch — the two are deliberately near-identical (issue identity +
    ``gh issue view`` directive + "NOT your task" worktree disclaimer).
    They cannot share a helper because this module is invoked standalone
    by the wrapper bash (``python3 .../event_prompt.py``) with no package
    context, so it cannot import ``egg_contracts``. The only intentional
    divergences are the ``(title)`` clause and the "no operator task
    statement was recorded" note, both specific to the fallback path.
    """
    issue = contract_data.get("issue")
    if not isinstance(issue, dict):
        return ""
    number = issue.get("number")
    if not isinstance(number, int):
        return ""
    anchor = f"This pipeline's task is GitHub issue #{number}"
    url = issue.get("url")
    if isinstance(url, str) and url.strip():
        anchor += f" — {url.strip()}"
    title = issue.get("title")
    if isinstance(title, str) and title.strip() and title.strip() != f"Issue #{number}":
        anchor += f" ({title.strip()})"
    anchor += (
        f". No operator task statement was recorded on this contract; "
        f"fetch the live issue body (`gh issue view {number}`) before "
        "structural decisions. Worktree artifacts (drafts, agent outputs) "
        "that reference any other issue or pipeline are leftovers from "
        "previous runs — they are NOT your task."
    )
    return anchor
