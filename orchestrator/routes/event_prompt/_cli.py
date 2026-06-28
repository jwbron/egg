"""Wrapper-bash CLI: ``python3 event_prompt/__main__.py <action>`` (slice-3 TASK-3-2).

Reads the action from argv, the event payload from stdin, the role /
base branch / repo path / memory mode from env, and prints the rendered
per-event prompt to stdout. Invoked standalone by the event-pump wrapper
bash so the heavy ``orchestrator.routes`` package ``__init__`` (Flask
import) is bypassed — see ``orchestrator/consensus_wrapper.py``. The
package's ``__main__.py`` is the entry point that calls :func:`_cli`.
AST-identical to the pre-split definitions — pure refactor (#3312
slice-6), except the docstring's invocation path now names the package
``__main__.py`` instead of the pre-split module file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ._compose import compose_event_prompt
from ._delta_builder import _build_delta_entries
from ._memory_io import (
    _memory_path,
    _pipeline_id_token,
    _read_memory_excerpt,
    _read_task_description,
)
from ._payload import _extract_iteration_feedback, _extract_nacks


def _context_discipline_enabled() -> bool:
    """Return whether the #3200 context discipline (master flag) is enabled.

    The single switch (``EGG_CONTEXT_DISCIPLINE``, default OFF) that flips THIS
    composer from the legacy full-context INLINE path to the
    queryable-environment JIT-pull path (``compose_event_prompt(jit_pull=…)``).
    Read once here in ``_cli`` and applied uniformly: the composer is already
    role-parameterized, so this one read covers every event-pump role —
    producers and reviewers alike — with no role hard-coding the new path.

    Canonical home: :func:`egg_agent.context_discipline.context_discipline_enabled`
    — the SAME function the sandbox-side warm-resume gate
    (``egg_agent.session.session_resume_enabled``) reads, so the whole discipline
    has one authoritative on/off. This module, however, also runs standalone
    under the wrapper bash where ``egg_agent`` may be off ``PYTHONPATH`` (the same
    constraint that forces :func:`_render_delta_pointer_section` to avoid the
    ``egg_agent`` import). So when the import is unavailable we fall back to
    reading the same env var inline with identical truthy semantics — a
    deliberate cross-boundary mirror, exactly the pattern
    ``egg_agent.reseed.resolve_reseed_threshold`` uses to import
    ``orchestrator.agent_model_resolution`` under ``try``/``except``.
    """
    try:
        from egg_agent.context_discipline import context_discipline_enabled
    except Exception:  # pragma: no cover - wrapper-bash standalone (egg_agent off path)
        return os.environ.get("EGG_CONTEXT_DISCIPLINE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    return context_discipline_enabled()


def _cli(argv: list[str] | None = None) -> int:
    """Render the per-event prompt for the wrapper bash (TASK-3-2).

    Reads the action from argv, the event payload from stdin, and the
    role / base branch / repo path / memory mode from env. Prints the
    rendered prompt to stdout. The wrapper bash invokes this as::

        prompt=$(printf '%s' "$event_payload" \\
            | python3 /opt/egg-runtime/orchestrator/routes/event_prompt/__main__.py \\
                "$action")

    Env vars consumed:

    * ``EGG_AGENT_ROLE`` (required) — agent role token; surfaces in
      role banner and gates the memory-file path.
    * ``EGG_BASE_BRANCH`` (default ``main``) — substituted into the
      ``--not origin/<base>`` term of the git-log delta.
    * ``EGG_REPO_PATH`` (default cwd) — working directory for the
      git-log subprocess + base for the memory-file path resolution.
    * ``EGG_BRC_MEMORY`` (default ``full`` since slice-4 task-4-1) —
      slice-1 reader gate; ``full`` enables the read path. Set
      ``write-only`` to keep the writer warm without reading the
      excerpt, or ``off`` for the one-release rollback escape hatch
      (no writes, no reads).
    * ``EGG_PIPELINE_ID`` / ``EGG_ISSUE_NUMBER`` (pod env, inherited —
      not re-exported by the wrapper) — contract identifier for the
      ``task_description`` section (#3123) and the pipeline-scoped
      memory filename (#3163). Unset → both sections omitted.
    * ``EGG_CONTEXT_DISCIPLINE`` (pod env, inherited — #3200 slice-9
      master flag, default OFF) — when truthy, renders the per-producer
      delta + durable memory as JIT-pull POINTERS instead of inlining
      the bulk (``jit_pull=True``); OFF keeps the legacy full-context
      inline path byte-for-byte. Read via ``_context_discipline_enabled``.
    """
    parser = argparse.ArgumentParser(
        description="Render the per-event BRC event-pump prompt (slice-3).",
    )
    parser.add_argument(
        "action",
        help="next-action verb: 'propose' | 'ack' | 'nack' | 'confirm' | 'complete'",
    )
    parser.add_argument(
        "--event-payload-file",
        default="",
        help="Path to a file containing the event_payload JSON (alternative to stdin).",
    )
    args = parser.parse_args(argv)

    role = (os.environ.get("EGG_AGENT_ROLE") or "").strip() or "unknown"
    base_branch = (os.environ.get("EGG_BASE_BRANCH") or "").strip() or "main"
    repo_path = Path(os.environ.get("EGG_REPO_PATH") or os.getcwd())
    # Slice-4 task-4-1 flipped the unset-env default from ``off`` to
    # ``full`` so the event-pump composer reads the memory file by
    # default. Operators can opt back into the slice-1 inert default
    # for a one-release rollback window by setting ``EGG_BRC_MEMORY=off``.
    memory_mode = (os.environ.get("EGG_BRC_MEMORY") or "full").strip().lower()

    # Event payload — JSON on stdin (preferred) or from --event-payload-file.
    if args.event_payload_file:
        try:
            event_raw = Path(args.event_payload_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"event_prompt: cannot read --event-payload-file: {exc}", file=sys.stderr)
            return 2
    else:
        event_raw = sys.stdin.read()
    event_raw = (event_raw or "").strip()
    if event_raw:
        try:
            event_payload = json.loads(event_raw)
        except json.JSONDecodeError:
            # Surface the raw payload so the agent can still see the
            # source string — better than silently treating it as
            # empty when the wrapper passed something we couldn't parse.
            event_payload = {"action": args.action, "raw": event_raw}
    else:
        event_payload = {"action": args.action}

    memory_path = _memory_path(repo_path, role)
    memory_text = _read_memory_excerpt(memory_path, memory_mode) if memory_path else ""
    # Even in ``write-only`` mode we still parse per-producer SHAs from
    # the on-disk file so the slice-3 wrapper renders the delta — the
    # mode gates only whether the markdown excerpt itself flows into
    # the prompt, not whether the per-producer SHAs flow into the
    # delta command. This matches the slice-3 plan TASK-3-2 wording:
    # "with ``EGG_BRC_MEMORY=write-only`` (slice-1 default), the prompt
    # omits memory but still emits the git-log delta against … a
    # fallback baseline".
    sha_lookup_text = memory_text
    if not sha_lookup_text and memory_path is not None and memory_path.exists():
        try:
            sha_lookup_text = memory_path.read_text(encoding="utf-8")
        except OSError:
            sha_lookup_text = ""

    delta_entries = _build_delta_entries(
        action=args.action,
        role=role,
        base_branch=base_branch,
        repo_path=repo_path,
        memory_text=sha_lookup_text,
        event_payload=event_payload,
    )

    nacks = _extract_nacks(event_payload)

    # Contract task statement (#3123) — pushed into every per-event
    # prompt so operator directives don't depend on the agent pulling
    # ``task_description`` per the rules file. Identifier comes from
    # ``EGG_PIPELINE_ID`` / ``EGG_ISSUE_NUMBER`` (pod env, inherited by
    # this subprocess); pre-#3163 issue contracts without a recorded
    # statement get a synthesized issue anchor; empty only when the
    # worktree lacks the contract file entirely (fail-soft).
    task_description = _read_task_description(repo_path)

    # Per-iteration operator kickback (#3231) — the orchestrator's
    # ``next-action`` route attaches ``iteration_feedback`` onto the
    # ``propose`` event_payload (sourced from
    # ``PhaseExecution.operator_directives`` / ``iteration_history``,
    # #2795) so the re-spawned producer addresses the operator's
    # ``request_changes`` / ``change_approach`` before re-proposing
    # instead of re-reading its own prior draft and re-proposing it
    # unchanged. Read straight off the event payload the wrapper pipes
    # on stdin — the composer subprocess has no orchestrator-package
    # context (see ``_read_task_description``) so the payload is the
    # only hop, mirroring how NACKs already flow.
    iteration_feedback = _extract_iteration_feedback(event_payload)

    # #3200 slice-9 (task-9-1): the single master flag gates the whole context
    # discipline. ON -> render the bulk (per-producer delta + memory) as
    # JIT-pull POINTERS so only the small role-parameterized protected root
    # stays resident; OFF (default) -> the legacy full-context INLINE path,
    # byte-for-byte unchanged. The composer is role-parameterized, so flipping
    # ``jit_pull`` here drives every event-pump role (producers AND reviewers)
    # through the new path without any role hard-coding it. ``memory_rel_path``
    # / ``pipeline_id`` are consumed only on the ``jit_pull=True`` arm (the
    # memory pointer + the ``brc-transcript`` pull handle); on the OFF arm they
    # are ignored, so the legacy path keeps no dependency on the new code.
    context_discipline = _context_discipline_enabled()
    memory_rel_path = ""
    if context_discipline and memory_path is not None:
        try:
            memory_rel_path = memory_path.relative_to(repo_path).as_posix()
        except ValueError:
            memory_rel_path = memory_path.as_posix()

    prompt = compose_event_prompt(
        role,
        event_payload if isinstance(event_payload, dict) else {"raw": event_payload},
        memory_text,
        nacks,
        delta_entries,
        base_branch,
        task_description=task_description,
        iteration_feedback=iteration_feedback,
        jit_pull=context_discipline,
        memory_rel_path=memory_rel_path,
        pipeline_id=_pipeline_id_token(),
    )
    sys.stdout.write(prompt)
    return 0
