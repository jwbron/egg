"""Slice-9 task-9-2 integration: the context-discipline feature flag, end-to-end.

#3200 slice-9 ("Generalize to ALL BRC roles behind a feature flag"). The coder
(task-9-1) introduces ONE feature flag, *read in one place*, gating the whole
#3200 context discipline (protected-root / queryable-environment split +
threshold reseed + JIT pull). The flag's contract — the behaviour this suite
pins end-to-end:

  * **OFF (and the staged-rollout default).** Every event-pump role takes
    today's full-context INLINE path, byte-for-byte unchanged: the per-producer
    ``git log`` diff and the 2 KB BRC-memory excerpt are inlined into the
    one-shot prompt exactly as before slice-9.
  * **ON.** Every event-pump role — producers AND reviewers — takes the new
    path: the bulk (diff + memory) moves to JIT-pull POINTERS
    (``read_peer_artifact`` / ``brc-transcript`` handles + the on-disk memory
    path), so only the small protected root stays resident. The mechanism is
    UNIFORM across roles (one flag, one code path); only the root CONTENT
    differs by role (the phase-4 role-parameterized renderer).

**Why drive the CLI.** These assertions exercise the real per-event prompt
composer through its CLI entry-point
(``orchestrator/routes/event_prompt.py::_cli``) — the exact seam the wrapper
bash shells out to, and the single place the flag flips ``jit_pull`` for every
role. That is the genuine integration surface for "one flag gates the whole
discipline for every role": a unit test of ``compose_event_prompt(jit_pull=...)``
would bypass the flag read entirely.

**Parallel-BRC convention.** Tester and coder run as parallel BRC producers on
separate branches, so task-9-1's flag may be absent when this file is collected
on the tester branch. Following the established slice convention (see
``test_queryable_env_jit.py`` / ``test_reseed_threshold.py``), the flag's env-var
NAME is the coder's to choose: the suite AUTO-DISCOVERS it by toggling candidate
names and observing which one flips the rendered path. When none flips (coder
task-9-1 unmerged) the flag-ON assertions skip; the flag-OFF byte-stability
assertions — true of the default path TODAY — run regardless and stay green
pre-merge, then continue to pass after convergence.

This module is composer-level and self-contained: it does NOT use the k3s
``egg_stack`` fixtures from ``conftest.py`` (no gateway / cluster needed).
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

# Add orchestrator to sys.path the same way the sibling slice tests do, so
# ``routes.event_prompt`` imports whether or not the package root is already on
# the path (the composer module is stdlib-only at import time — no Flask).
_ORCH_PATH = Path(__file__).resolve().parent.parent / "orchestrator"
if str(_ORCH_PATH) not in sys.path:
    sys.path.insert(0, str(_ORCH_PATH))


# ---------------------------------------------------------------------------
# Locator: the per-event prompt CLI. The composer lives in
# orchestrator/routes/event_prompt.py; cover the plausible import spellings so
# the probe binds wherever it is reachable from.
# ---------------------------------------------------------------------------

_CLI_MODULE_CANDIDATES: tuple[str, ...] = (
    "orchestrator.routes.event_prompt",
    "routes.event_prompt",
    "event_prompt",
)


def _cli_module() -> Any:
    for name in _CLI_MODULE_CANDIDATES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if callable(getattr(module, "_cli", None)):
            return module
    pytest.skip(
        f"event-prompt CLI module not found; tried {list(_CLI_MODULE_CANDIDATES)}"
    )


# Candidate env-var names for the single slice-9 master flag. The coder owns the
# exact spelling; these cover the plausible ones so discovery binds wherever it
# lands. Discovery is behavioural (which name flips the path), so an extra
# never-used candidate is harmless.
_FLAG_CANDIDATES: tuple[str, ...] = (
    "EGG_CONTEXT_DISCIPLINE",
    "EGG_BRC_CONTEXT_DISCIPLINE",
    "EGG_CONTEXT_DISCIPLINE_ENABLED",
    "EGG_BRC_CONTEXT_DISCIPLINE_ENABLED",
    "EGG_RESIDENT_ROOT",
    "EGG_BRC_RESIDENT_ROOT",
    "EGG_PROTECTED_ROOT",
    "EGG_BRC_PROTECTED_ROOT",
    "EGG_CONTEXT_ROOT",
    "EGG_RETRIEVAL_ROOT",
    "EGG_JIT_PULL",
    "EGG_BRC_JIT_PULL",
)

# Env vars the harness must NEUTRALISE before each run so an ambient flag (a
# leftover EGG_SESSION_RESUME, a candidate the operator exported, etc.) cannot
# perturb the baseline. The required-for-the-run vars are set explicitly inside
# the context manager; everything else flag-adjacent is cleared.
_MANAGED_ENV: tuple[str, ...] = (
    "EGG_AGENT_ROLE",
    "EGG_BASE_BRANCH",
    "EGG_REPO_PATH",
    "EGG_BRC_MEMORY",
    "EGG_PIPELINE_ID",
    "EGG_ISSUE_NUMBER",
    "EGG_EVENT_PROMPT_SCRIPT",
    "EGG_SESSION_RESUME",
    "EGG_SESSION_STATE_FILE",
    "EGG_RESEED_THRESHOLD",
    *_FLAG_CANDIDATES,
)

_PIPELINE_ID = "issue-3200"

# A unique multi-byte memory sentinel. If it appears in the rendered prompt the
# 2 KB memory EXCERPT is being inlined (legacy path); if it is gone the bulk has
# moved out (new path).
_MEMORY_SENTINEL = "ZZ-MEMORY-INLINE-SENTINEL-SLICE9-ZZ"
_MEMORY_BODY = (
    "# BRC memory\n\n"
    "## codebase / change model\n\n"
    f"{_MEMORY_SENTINEL} distilled state across prior events for this slice.\n"
)

# Reviewer event: an ACK naming one producer with a pushed proposal SHA + an
# artifact ref. No stored last-reviewed SHA (the reviewer has no memory file in
# the tmp repo), so the composer takes the artifact-fallback baseline — NO git
# subprocess is run, keeping the test hermetic.
_PROPOSAL_SHA = "def5678abcdef0123456789abcdef0123456789ab"
_REVIEW_PAYLOAD: dict[str, Any] = {
    "action": "ack",
    "pending_reviews": [
        {
            "producer": "coder",
            "proposal_commit_sha": _PROPOSAL_SHA,
            "artifact_refs": ["shared/egg_agent/client.py"],
        }
    ],
}
# Producer event: a propose. No per-producer delta (action is not ack/nack), so
# the ON-vs-OFF difference for a producer is purely the memory section
# (inline excerpt -> on-disk pointer).
_PRODUCER_PAYLOAD: dict[str, Any] = {"action": "propose", "producer": "coder", "version": 1}

# Tokens that appear ONLY on the JIT-pull (flag-ON) path. The legacy inline
# delta header is "## Per-producer re-review delta" (no "(pull on demand)"); the
# JIT header adds "(pull on demand)" and names the served-read handles.
_JIT_TOKENS: tuple[str, ...] = ("read_peer_artifact", "brc-transcript", "(pull on demand)")


def _has_jit_tokens(prompt: str) -> bool:
    return any(token in prompt for token in _JIT_TOKENS)


@contextlib.contextmanager
def _managed_env(role: str, repo: Path, overrides: dict[str, str]) -> Iterator[None]:
    """Run with a clean, explicit env for one ``_cli`` invocation; restore after."""
    import os

    saved = {k: os.environ.get(k) for k in _MANAGED_ENV}
    try:
        for k in _MANAGED_ENV:
            os.environ.pop(k, None)
        os.environ["EGG_AGENT_ROLE"] = role
        os.environ["EGG_BASE_BRANCH"] = "main"
        os.environ["EGG_REPO_PATH"] = str(repo)
        os.environ["EGG_BRC_MEMORY"] = "full"
        os.environ["EGG_PIPELINE_ID"] = _PIPELINE_ID
        os.environ.update(overrides)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_memory(repo: Path, role: str) -> None:
    """Seed the role's pipeline-scoped BRC memory file (mirrors ``_memory_path``)."""
    mem = repo / ".egg-state" / "agent-outputs" / role / f"brc-memory-{_PIPELINE_ID}.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(_MEMORY_BODY, encoding="utf-8")


def _run_cli(
    role: str,
    payload: dict[str, Any],
    repo: Path,
    overrides: dict[str, str] | None = None,
) -> str:
    """Drive ``event_prompt._cli`` for one event; return the rendered prompt.

    Uses ``--event-payload-file`` (the documented stdin alternative) so no
    stdin monkeypatching is needed. Returns the captured stdout.
    """
    module = _cli_module()
    cli: Callable[[list[str]], int] = module._cli
    action = str(payload.get("action") or "propose")
    payload_file = repo / f"_event_{role}_{action}.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")

    buf = io.StringIO()
    with _managed_env(role, repo, overrides or {}):
        with contextlib.redirect_stdout(buf):
            rc = cli([action, "--event-payload-file", str(payload_file)])
    assert rc == 0, f"_cli returned {rc} for role={role} action={action}"
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo root carrying the producer role's memory file."""
    _write_memory(tmp_path, "coder")
    return tmp_path


@pytest.fixture(scope="module")
def discovered_flag(tmp_path_factory: pytest.TempPathFactory) -> str | None:
    """The master flag env-var name, discovered behaviourally — or ``None``.

    Renders the reviewer ACK prompt with each candidate name set truthy and
    returns the first whose presence flips the path to JIT-pull while the
    no-flag baseline does NOT. ``None`` => coder task-9-1 is unmerged on this
    branch (no candidate flips), so the flag-ON tests skip.
    """
    base_repo = tmp_path_factory.mktemp("flag-discovery")
    _write_memory(base_repo, "coder")
    baseline = _run_cli("reviewer_code", _REVIEW_PAYLOAD, base_repo, {})
    if _has_jit_tokens(baseline):
        # The default already renders JIT pointers — that contradicts the
        # OFF-is-default contract; let the dedicated OFF test report it rather
        # than mis-identify a flag here.
        return None
    for name in _FLAG_CANDIDATES:
        out = _run_cli("reviewer_code", _REVIEW_PAYLOAD, base_repo, {name: "1"})
        if _has_jit_tokens(out):
            return name
    return None


# ---------------------------------------------------------------------------
# Flag-OFF: the legacy full-context path is unchanged (runs TODAY).
# ---------------------------------------------------------------------------


def test_default_reviewer_path_is_legacy_inline(repo: Path) -> None:
    """With no flag set, a reviewer ACK inlines the delta — no JIT pointers.

    The rollout default is OFF: the legacy inline path. The prompt carries the
    inline delta header and names none of the JIT-pull handles.
    """
    prompt = _run_cli("reviewer_code", _REVIEW_PAYLOAD, repo, {})
    assert prompt, "composer produced an empty reviewer prompt"
    assert not _has_jit_tokens(prompt), (
        "default (flag-OFF) reviewer prompt already renders JIT-pull pointers; "
        "the OFF path must be the legacy inline path"
    )
    assert "## Per-producer re-review delta" in prompt, (
        "default reviewer prompt is missing the legacy inline delta header"
    )


def test_default_producer_path_inlines_memory(repo: Path) -> None:
    """With no flag set, a producer propose inlines the BRC-memory excerpt.

    The memory sentinel is present verbatim (excerpt inlined) and no JIT-pull
    pointer is rendered — the pre-slice-9 behaviour.
    """
    prompt = _run_cli("coder", _PRODUCER_PAYLOAD, repo, {})
    assert _MEMORY_SENTINEL in prompt, (
        "default (flag-OFF) producer prompt did not inline the memory excerpt"
    )
    assert not _has_jit_tokens(prompt), (
        "default (flag-OFF) producer prompt rendered JIT-pull pointers"
    )


def test_falsey_flag_spellings_are_byte_identical_to_default(repo: Path) -> None:
    """No falsey spelling of any candidate flag flips the path off-default.

    For every candidate name set to a falsey value, the rendered prompt — for
    BOTH a reviewer and a producer — must be byte-identical to the no-flag
    default. This pins that the OFF state preserves the legacy path exactly
    (byte-for-byte) and that an accidental ``=0`` / ``=false`` never enables the
    new discipline. Runs today (the default is already OFF) and keeps holding
    after the coder wires the real flag.
    """
    reviewer_default = _run_cli("reviewer_code", _REVIEW_PAYLOAD, repo, {})
    producer_default = _run_cli("coder", _PRODUCER_PAYLOAD, repo, {})
    for name in _FLAG_CANDIDATES:
        for falsey in ("0", "false", "no", "off", ""):
            assert (
                _run_cli("reviewer_code", _REVIEW_PAYLOAD, repo, {name: falsey})
                == reviewer_default
            ), f"reviewer prompt changed with falsey {name}={falsey!r}"
            assert (
                _run_cli("coder", _PRODUCER_PAYLOAD, repo, {name: falsey})
                == producer_default
            ), f"producer prompt changed with falsey {name}={falsey!r}"


# ---------------------------------------------------------------------------
# Flag-ON: every role takes the new path (skip-guarded on discovery).
# ---------------------------------------------------------------------------


def test_flag_on_reviewer_uses_jit_pull(repo: Path, discovered_flag: str | None) -> None:
    """Flag-ON: a reviewer's bulk delta moves to JIT-pull pointers.

    Covers the ">=1 reviewer" half of the AC: with the master flag ON the
    reviewer ACK prompt names the JIT-pull handles and drops the legacy inline
    delta header.
    """
    if discovered_flag is None:
        pytest.skip("master context-discipline flag not wired yet (coder task-9-1 unmerged)")
    prompt = _run_cli("reviewer_code", _REVIEW_PAYLOAD, repo, {discovered_flag: "1"})
    assert _has_jit_tokens(prompt), (
        f"flag-ON ({discovered_flag}) reviewer prompt names no JIT-pull handle"
    )
    assert "read_peer_artifact" in prompt or "brc-transcript" in prompt, (
        "flag-ON reviewer prompt does not steer the agent to a served-read pull tool"
    )


def test_flag_on_producer_excludes_inline_bulk(
    repo: Path, discovered_flag: str | None
) -> None:
    """Flag-ON: a producer's BRC-memory excerpt is no longer inlined.

    Covers the ">=1 producer" half of the AC: with the flag ON the producer
    propose prompt no longer carries the inline memory sentinel — the bulk has
    moved into the queryable environment.
    """
    if discovered_flag is None:
        pytest.skip("master context-discipline flag not wired yet (coder task-9-1 unmerged)")
    prompt = _run_cli("coder", _PRODUCER_PAYLOAD, repo, {discovered_flag: "1"})
    assert _MEMORY_SENTINEL not in prompt, (
        f"flag-ON ({discovered_flag}) producer prompt still inlines the memory excerpt; "
        "the bulk must move out of the resident prompt"
    )


def test_flag_on_mechanism_is_uniform_across_roles(
    repo: Path, discovered_flag: str | None
) -> None:
    """One flag value flips BOTH a producer and a reviewer to the new path.

    The "uniform mechanism" AC: the SAME master flag (read in one place) drives
    every event-pump role through the split — the reviewer gains JIT pointers
    AND the producer drops its inline bulk under a single flag value, with no
    per-role opt-in.
    """
    if discovered_flag is None:
        pytest.skip("master context-discipline flag not wired yet (coder task-9-1 unmerged)")
    reviewer_on = _run_cli("reviewer_code", _REVIEW_PAYLOAD, repo, {discovered_flag: "1"})
    producer_on = _run_cli("coder", _PRODUCER_PAYLOAD, repo, {discovered_flag: "1"})
    assert _has_jit_tokens(reviewer_on), "reviewer did not switch to the new path under the flag"
    assert _MEMORY_SENTINEL not in producer_on, (
        "producer did not switch to the new path under the same flag — mechanism is not uniform"
    )


def test_flag_on_root_is_role_parameterized(
    repo: Path, discovered_flag: str | None
) -> None:
    """Flag-ON roots are role-parameterized: distinct per role, each non-empty.

    The mechanism is uniform but the CONTENT differs by role: the producer's
    prompt names its own role and differs from the reviewer's. This pins the
    phase-4 role-parameterized renderer is what drives the per-role root.
    """
    if discovered_flag is None:
        pytest.skip("master context-discipline flag not wired yet (coder task-9-1 unmerged)")
    reviewer_on = _run_cli("reviewer_code", _REVIEW_PAYLOAD, repo, {discovered_flag: "1"})
    producer_on = _run_cli("coder", _PRODUCER_PAYLOAD, repo, {discovered_flag: "1"})
    assert reviewer_on and producer_on, "a role rendered an empty root under the flag"
    assert "reviewer_code" in reviewer_on, "reviewer root does not name its own role"
    assert "coder" in producer_on, "producer root does not name its own role"
    assert reviewer_on != producer_on, (
        "producer and reviewer rendered identical roots — root is not role-parameterized"
    )
