"""Per-event prompt composer for the BRC event-pump (#2908 slice-3 task-3-1).

The slice-2 event-pump wrapper (``orchestrator/consensus_wrapper.py``)
invokes the agent one-shot per actionable BRC event. Slice-2 shipped a
minimal stub prompt; this module is the slice-3 replacement.

The composer assembles the single user prompt the wrapper hands to
``python3 -m egg_agent`` for a given event. Memory continuity rides on
the durable per-role memory artifact written by slice-1
(``sandbox/egg_agent_tools/handlers/brc_memory.py``); for review events
the prompt also includes the FULL ``git log
{last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p`` delta
per producer so the re-review audits the actual delta — not just the
orchestrator-side ``changed_artifacts`` summary, which would
systematically weaken adversarial re-review (see
``shared/prompts/REVIEWER-SYNC.md`` + risk_analyst R6 from
the replan2 architect output).

Design choices encoded here:

* **Memory at tail position (architect od-6 Option B).** The memory
  excerpt is appended at the very end of the user prompt rather than
  passed through ``--append-context`` (which does not exist on
  ``build_agent_command`` at
  ``shared/egg_agent/command.py:11-46``). Tail position keeps the
  surrounding event-specific framing in the cacheable prefix while
  letting the memory bytes change without invalidating the cache for
  the prior turns.

* **Envelope budget (≤ 10 KB) excludes the git-log delta.** The delta
  scales with the actual change size; capping it would defeat the
  whole point of full-delta re-review. The 10 KB cap bounds only the
  surrounding prose so a chatty NACK reason can't push the cacheable
  prefix past a healthy size.

* **Memory excerpt cap at 2 KB.** Matches the slice-1 writer's caps on
  the codebase prose (2 KB) and per-producer summary (1 KB each) so a
  well-distilled file lands inside the budget; an over-stuffed file is
  truncated rather than rejected.

* **No ``changed_artifacts``-only shortcut.** The git-log delta MUST be
  the full ``git log {sha}..HEAD --not origin/{base_branch} -p`` output
  per producer. The composer renders the command verbatim alongside the
  rendered diff so the agent can audit the scope without re-deriving it.

Decomposition note (#3312 slice-6): the pre-split single-file module is
now a sub-package. This ``__init__.py`` is the **stable public API
barrel** — every externally-referenced and ``unittest.mock.patch``-target
symbol re-exports here so ``from routes.event_prompt import _foo`` and
``patch("routes.event_prompt._foo")`` keep resolving unchanged. The
implementation lives in underscore-prefixed private submodules:

- ``_caps``          — envelope/excerpt caps, sentinels, ``_truncate``, regexes
- ``_render_event``  — role banner + JSON event section
- ``_render_delta``  — per-producer delta (inline + JIT-pull pointer)
- ``_render_nacks``  — open-NACK barrier section
- ``_render_memory`` — durable-memory section (inline + JIT-pull pointer)
- ``_render_task``   — task_description / iteration-feedback / issue-anchor
- ``_compose``       — ``compose_event_prompt`` + envelope-cap pass
- ``_payload``       — event-payload extractors
- ``_memory_io``     — worktree contract/memory IO + path resolution
- ``_delta_builder`` — ``git log`` subprocess + per-producer delta entries
- ``_cli``           — wrapper-bash CLI (``_cli`` / ``_context_discipline_enabled``)

The standalone wrapper-bash entry point is ``__main__.py`` (the wrapper
runs ``python3 .../event_prompt/__main__.py <action>``), which bootstraps
``sys.path`` and calls :func:`_cli` — bypassing the heavy
``orchestrator.routes`` package ``__init__`` (Flask) exactly as the
pre-split standalone-script invocation did.

Pure refactor: every re-exported symbol is AST-identical to the
pre-split definition. ``_build_delta_entries`` calls ``_run_git_log``
through this package module object so the ``patch("…event_prompt.
_run_git_log")`` seam keeps intercepting it.
"""

from __future__ import annotations

from ._caps import (
    _ENVELOPE_TRUNCATION_SENTINEL,
    _GIT_LOG_DELTA_MAX_BYTES,
    _GIT_LOG_TIMEOUT_SECS,
    _ITERATION_FEEDBACK_TRUNCATION_SENTINEL,
    _LAST_REVIEWED_SHA_RE,
    _MEMORY_MODE_FULL,
    _NACK_PAYLOAD_KEYS,
    _PRODUCER_HEADING_RE,
    _TASK_TRUNCATION_SENTINEL,
    ITERATION_FEEDBACK_MAX_CHARS,
    MEMORY_EXCERPT_MAX_CHARS,
    PROMPT_ENVELOPE_MAX_BYTES,
    TASK_DESCRIPTION_MAX_CHARS,
    _truncate,
)
from ._cli import _cli, _context_discipline_enabled
from ._compose import compose_event_prompt
from ._delta_builder import _build_delta_entries, _run_git_log
from ._memory_io import (
    _memory_path,
    _parse_per_producer_sha,
    _pipeline_id_token,
    _read_memory_excerpt,
    _read_task_description,
)
from ._payload import (
    _extract_artifacts_for_producer,
    _extract_changed_artifacts,
    _extract_current_producers,
    _extract_iteration_feedback,
    _extract_nacks,
    _extract_producer_role,
    _extract_proposal_sha_for_producer,
)
from ._render_delta import _render_delta_pointer_section, _render_producer_delta_section
from ._render_event import _render_event_section, _strip_nacks_for_json
from ._render_memory import _render_memory_pointer_section, _render_memory_section
from ._render_nacks import _render_nacks_section
from ._render_task import (
    _directive_meta_tag,
    _issue_anchor_fallback,
    _render_iteration_feedback_section,
    _render_task_section,
)

__all__ = [
    # Caps / sentinels / helpers (_caps)
    "ITERATION_FEEDBACK_MAX_CHARS",
    "MEMORY_EXCERPT_MAX_CHARS",
    "PROMPT_ENVELOPE_MAX_BYTES",
    "TASK_DESCRIPTION_MAX_CHARS",
    "_ENVELOPE_TRUNCATION_SENTINEL",
    "_GIT_LOG_DELTA_MAX_BYTES",
    "_GIT_LOG_TIMEOUT_SECS",
    "_ITERATION_FEEDBACK_TRUNCATION_SENTINEL",
    "_LAST_REVIEWED_SHA_RE",
    "_MEMORY_MODE_FULL",
    "_NACK_PAYLOAD_KEYS",
    "_PRODUCER_HEADING_RE",
    "_TASK_TRUNCATION_SENTINEL",
    "_truncate",
    # Composer (_compose)
    "compose_event_prompt",
    # Section renderers (_render_*)
    "_strip_nacks_for_json",
    "_render_event_section",
    "_render_producer_delta_section",
    "_render_delta_pointer_section",
    "_render_nacks_section",
    "_render_memory_section",
    "_render_memory_pointer_section",
    "_render_task_section",
    "_directive_meta_tag",
    "_render_iteration_feedback_section",
    "_issue_anchor_fallback",
    # Event-payload extractors (_payload)
    "_extract_changed_artifacts",
    "_extract_current_producers",
    "_extract_proposal_sha_for_producer",
    "_extract_artifacts_for_producer",
    "_extract_producer_role",
    "_extract_nacks",
    "_extract_iteration_feedback",
    # Worktree IO + memory parsing (_memory_io)
    "_read_task_description",
    "_parse_per_producer_sha",
    "_pipeline_id_token",
    "_memory_path",
    "_read_memory_excerpt",
    # Delta builder (_delta_builder)
    "_run_git_log",
    "_build_delta_entries",
    # CLI (_cli)
    "_context_discipline_enabled",
    "_cli",
]
