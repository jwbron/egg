"""Tests for ``PreToolUseHookPolicy`` (#2623 slice-1 task-1-4, task-1-8).

Acceptance criteria covered:

* ``PreToolUseHookPolicy.check_write(role, path)`` denies out-of-role
  writes — match behavior with
  ``gateway/phase_filter.py:1061 check_agent_restrictions``.
* The accompanying ``hook_entry.decide`` function returns
  ``{"decision": "block", "reason": ...}`` for blocked tool calls,
  ``{}`` for allowed.
* The hook entry script reads JSON on stdin and prints JSON on stdout
  per the Claude Code PreToolUse hook protocol.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)
policy_mod = pytest.importorskip(
    "orchestrator.substrate.claude_code.policy",
    reason="orchestrator/substrate/claude_code/policy.py not present yet",
)
hook_entry_mod = pytest.importorskip(
    "orchestrator.substrate.claude_code.hook_entry",
    reason="orchestrator/substrate/claude_code/hook_entry.py not present yet",
)


# ---------------------------------------------------------------------------
# check_write — in-process enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,blocked_path,note",
    [
        # Tester cannot write source files.
        ("tester", "orchestrator/concurrent_executor.py", "tester→source"),
        # Coder cannot write docs.
        ("coder", "docs/architecture/claude-code-substrate.md", "coder→docs"),
        # Documenter cannot write source.
        ("documenter", "orchestrator/concurrent_executor.py", "documenter→source"),
    ],
)
def test_check_write_denies_out_of_role_writes(role: str, blocked_path: str, note: str) -> None:
    """``check_write`` returns ``(False, reason)`` for blocked role+path combos."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    allowed, reason = policy.check_write(role, blocked_path)
    assert allowed is False, f"{note}: expected denial; got allowed=True"
    assert reason, f"{note}: denial must carry a non-empty reason"


@pytest.mark.parametrize(
    "role,allowed_path",
    [
        ("tester", "shared/tests/test_substrate_interfaces.py"),
        ("coder", "orchestrator/substrate/spawner.py"),
        ("documenter", "docs/architecture/claude-code-substrate.md"),
    ],
)
def test_check_write_allows_in_role_writes(role: str, allowed_path: str) -> None:
    """``check_write`` returns ``(True, None)`` for in-role writes."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    allowed, reason = policy.check_write(role, allowed_path)
    assert allowed is True, f"{role}→{allowed_path} expected allowed; got reason={reason!r}"
    assert reason is None


def test_check_write_no_role_allows_everything() -> None:
    """When ``role`` is empty the policy fail-opens."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    allowed, reason = policy.check_write("", "orchestrator/anything.py")
    assert allowed is True
    assert reason is None


# ---------------------------------------------------------------------------
# decide() — hook-shape contract (block vs allow)
# ---------------------------------------------------------------------------


def test_decide_blocks_out_of_role_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hook_entry.decide`` returns ``{"decision": "block", ...}`` on denial."""
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_REPO_ROOT", "/home/egg/repos/egg")
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/home/egg/repos/egg/orchestrator/concurrent_executor.py",
            "content": "hi",
        },
    }
    result = hook_entry_mod.decide(payload)
    assert result.get("decision") == "block", (
        f"hook_entry.decide must block tester→source write; got {result!r}"
    )
    assert "reason" in result and result["reason"]


def test_decide_allows_in_role_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hook_entry.decide`` returns ``{}`` for in-role writes (allow)."""
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_REPO_ROOT", "/home/egg/repos/egg")
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": ("/home/egg/repos/egg/shared/tests/test_substrate_interfaces.py"),
            "content": "import pytest",
        },
    }
    result = hook_entry_mod.decide(payload)
    assert result == {} or "decision" not in result, (
        f"hook_entry.decide must allow tester→test-file; got {result!r}"
    )


def test_decide_fail_open_when_role_not_set_outside_substrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``EGG_AGENT_ROLE`` AND outside substrate prefixes, fail-open.

    v2 security NACK changed the semantics: when the role is unresolved
    and the write target is outside the substrate-managed prefixes
    (``.egg-state/``, ``.claude/``, ``.github/``,
    ``shared/egg_restrictions/``), the hook still fail-opens so a plain
    Claude Code session isn't affected.  Inside a substrate prefix the
    hook now fails closed — covered by
    :func:`test_decide_fails_closed_when_role_not_set_inside_substrate`.
    """
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    # The role resolver also reads a $HOME/.claude/egg-active-role.json
    # sentinel; point HOME at a clean tmp_path so the sentinel isn't
    # present.
    clean_home = tmp_path / "clean-home"
    clean_home.mkdir()
    monkeypatch.setenv("HOME", str(clean_home))
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/tmp/some-non-substrate-file.txt",
            "content": "...",
        },
    }
    result = hook_entry_mod.decide(payload)
    assert result == {} or "decision" not in result, (
        f"Without EGG_AGENT_ROLE and outside substrate prefixes the hook "
        f"must fail-open; got {result!r}"
    )


def test_decide_fails_closed_when_role_not_set_inside_substrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2 security NACK: hook fails CLOSED inside substrate-managed prefixes."""
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    clean_home = tmp_path / "clean-home"
    clean_home.mkdir()
    monkeypatch.setenv("HOME", str(clean_home))
    monkeypatch.setenv("EGG_REPO_ROOT", str(tmp_path))
    (tmp_path / ".egg-state").mkdir()
    target = tmp_path / ".egg-state" / "drafts" / "analysis.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": "...",
        },
    }
    result = hook_entry_mod.decide(payload)
    assert result.get("decision") == "block", (
        f"Without EGG_AGENT_ROLE the hook must fail-closed inside "
        f"substrate-managed prefixes (.egg-state/); got {result!r}"
    )


def test_decide_ignores_pure_read_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pure-read tools (``Read``) get no decision (allow).

    Note: ``Bash`` is NOT a read-only tool in this hook's model — the
    hook inspects Bash commands for write-shaped tokens (redirection,
    cp/mv/tee/sed -i/dd of=) and may block on the parsed write target.
    See ``test_bash_write_extraction_blocks_out_of_role`` for the
    Bash-side coverage.
    """
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/anything"},
    }
    result = hook_entry_mod.decide(payload)
    assert result == {} or "decision" not in result


# ---------------------------------------------------------------------------
# Hook entry script: stdin JSON → stdout JSON contract
# ---------------------------------------------------------------------------


def test_hook_entry_script_blocks_out_of_role_write(
    tmp_path: Path,
) -> None:
    """The hook entry script emits ``decision=block`` on stdout for blocked calls."""
    hook_entry = Path("/home/egg/repos/egg/orchestrator/substrate/claude_code/hook_entry.py")
    if not hook_entry.exists():
        pytest.skip(f"{hook_entry} not present")

    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/home/egg/repos/egg/orchestrator/concurrent_executor.py",
            "content": "hi",
        },
    }
    env = dict(os.environ)
    env["EGG_AGENT_ROLE"] = "tester"
    env["EGG_REPO_ROOT"] = "/home/egg/repos/egg"
    # The script may need to import egg_restrictions; thread the
    # project root onto sys.path via PYTHONPATH.
    env["PYTHONPATH"] = (
        "/home/egg/repos/egg/shared"
        + os.pathsep
        + "/home/egg/repos/egg/orchestrator"
        + os.pathsep
        + "/home/egg/repos/egg"
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [sys.executable, str(hook_entry)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    # The Claude Code hook protocol carries the decision in stdout
    # JSON, not the exit code. Exit code 0 is the normal "I ran"
    # signal; the block decision is in stdout.
    assert proc.returncode == 0, (
        f"hook script must exit 0 on normal completion; got rc={proc.returncode}, "
        f"stderr={proc.stderr!r}"
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"hook script stdout must be valid JSON; got {proc.stdout!r} ({exc})")
    assert out.get("decision") == "block", (
        f"hook script must emit decision=block for tester→source write; got stdout={proc.stdout!r}"
    )


def test_hook_entry_script_allows_in_role_write(tmp_path: Path) -> None:
    """The hook entry script emits ``{}`` for in-role writes (allow)."""
    hook_entry = Path("/home/egg/repos/egg/orchestrator/substrate/claude_code/hook_entry.py")
    if not hook_entry.exists():
        pytest.skip(f"{hook_entry} not present")

    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": ("/home/egg/repos/egg/shared/tests/test_substrate_interfaces.py"),
            "content": "...",
        },
    }
    env = dict(os.environ)
    env["EGG_AGENT_ROLE"] = "tester"
    env["EGG_REPO_ROOT"] = "/home/egg/repos/egg"
    env["PYTHONPATH"] = (
        "/home/egg/repos/egg/shared"
        + os.pathsep
        + "/home/egg/repos/egg/orchestrator"
        + os.pathsep
        + "/home/egg/repos/egg"
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [sys.executable, str(hook_entry)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out == {} or "decision" not in out, (
        f"hook script must allow tester→test-file; got stdout={proc.stdout!r}"
    )


# ---------------------------------------------------------------------------
# install() — settings.json templating
# ---------------------------------------------------------------------------


def _install_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build an ``install``-acceptable target dir under a faked ``$HOME``.

    The v2 ``install`` adds a path-escape guard that refuses any target
    outside ``$HOME``. Point ``$HOME`` at a tmp_path subdir so the test
    exercises the happy path without polluting the real ``$HOME``.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    target = fake_home / "repo"
    target.mkdir()
    return target


def test_install_writes_settings_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``install`` writes ``.claude/settings.json`` containing the hook."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    target = _install_target(tmp_path, monkeypatch)
    out = policy.install(target)
    assert out.exists()
    settings = json.loads(out.read_text())
    assert "hooks" in settings, "settings.json must include a 'hooks' block"


def test_install_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running ``install`` does not duplicate the egg hook."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    target = _install_target(tmp_path, monkeypatch)
    out1 = policy.install(target)
    first = json.loads(out1.read_text())
    policy.install(target)  # second run
    second = json.loads(out1.read_text())
    assert first == second, "Repeated install() calls must produce byte-identical settings.json"


def test_install_rejects_target_outside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2 security guard: ``install`` refuses target_dir outside ``$HOME``."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match=r"not under \$HOME"):
        policy.install(outside)


# ---------------------------------------------------------------------------
# v3 install fail-loud on malformed existing settings.json (reviewer_code v2)
# ---------------------------------------------------------------------------


def test_install_raises_on_malformed_existing_settings_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed JSON in the existing ``.claude/settings.json`` raises loud.

    v3 fix: instead of silently overwriting the user's prior hooks /
    statusline / plugin enablement, ``install`` raises ``ValueError``
    pointing the operator at the bad file with line + column from
    ``json.JSONDecodeError``.
    """
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    target = _install_target(tmp_path, monkeypatch)
    settings = target / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text("this is not json")
    with pytest.raises(ValueError, match=r"not valid\s+JSON"):
        policy.install(target)


def test_install_raises_on_non_dict_top_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON array at the top level is rejected (must be a JSON object)."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    target = _install_target(tmp_path, monkeypatch)
    settings = target / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text("[]")
    with pytest.raises(ValueError, match=r"not a JSON\s+object"):
        policy.install(target)


def test_install_accepts_empty_existing_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty / whitespace-only existing settings.json is treated as ``{}``."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    target = _install_target(tmp_path, monkeypatch)
    settings = target / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text("   \n  ")
    out = policy.install(target)  # must not raise
    contents = json.loads(out.read_text())
    assert "hooks" in contents


# ---------------------------------------------------------------------------
# v2 security NACK #1: Bash command write-target parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,note",
    [
        ("echo hi > orchestrator/concurrent_executor.py", "redirect >"),
        ("echo hi >> orchestrator/concurrent_executor.py", "append >>"),
        ("cp data.txt orchestrator/concurrent_executor.py", "cp dest"),
        ("mv old.py orchestrator/concurrent_executor.py", "mv dest"),
        ("tee orchestrator/concurrent_executor.py", "tee target"),
        ("sed -i s/a/b/ orchestrator/concurrent_executor.py", "sed -i"),
        ("dd if=/dev/zero of=orchestrator/concurrent_executor.py", "dd of="),
        # Reviewer v3 non-blocking: short-flag-cluster bash recursion.
        # ``bash -lc`` / ``-xc`` / ``-ic`` carry the ``-c`` mode along
        # with other single-char options. The hook must still recurse
        # into the inner command so the inner write is surfaced.
        (
            "bash -lc 'echo x > orchestrator/concurrent_executor.py'",
            "bash -lc cluster",
        ),
        (
            "bash -xc 'echo x > orchestrator/concurrent_executor.py'",
            "bash -xc cluster",
        ),
        (
            "sh -ic 'echo x > orchestrator/concurrent_executor.py'",
            "sh -ic cluster",
        ),
        # Reviewer v3 non-blocking: tar long-form ``--extract`` must
        # surface the ``-C`` target.
        (
            "tar --extract -f archive.tar -C orchestrator/",
            "tar --extract -C",
        ),
        # Reviewer v3 non-blocking: tar short-flag cluster ``-xzf``
        # surfaces the ``-C`` target.
        (
            "tar -xzf archive.tar.gz -C orchestrator/",
            "tar -xzf -C",
        ),
    ],
)
def test_bash_write_extraction_blocks_out_of_role(
    command: str,
    note: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bash commands writing to source code are denied for tester role.

    Mirrors v2 security NACK #1: the hook parses Bash for redirection
    + write-shaped tokens (cp / mv / tee / sed -i / dd of= / ln -s /
    python -c "open(...).write(...)").  Each write-shape gets a
    dedicated test so a future parser regression localizes quickly.

    Reviewer v3 non-blocking widened the parametrize set to cover the
    tar long-form / short-flag-cluster extract detection and the
    ``bash -lc`` / ``-xc`` / ``-ic`` combined-short-flag recursion;
    those code paths were added in commit ``8ce6b28`` and previously
    had no regression test.
    """
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_REPO_ROOT", "/home/egg/repos/egg")
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    result = hook_entry_mod.decide(payload)
    assert result.get("decision") == "block", (
        f"{note}: Bash write to source must be denied for tester; command={command!r} → {result!r}"
    )


@pytest.mark.parametrize(
    "command,note",
    [
        # Reviewer v3 non-blocking: tar long-form ``--xattrs`` /
        # ``--xz`` / ``--exclude=*`` are NOT extracts and must not
        # trip the extract-mode false-positive that v3's
        # ``_is_tar_extract`` helper closed. The commands all create
        # rather than extract, so a tester running them outside an
        # ``-x`` mode should not surface a phantom write target.
        ("tar --xattrs -cf /tmp/out.tar /tmp/src", "tar --xattrs"),
        ("tar -cf /tmp/out.tar.xz --xz /tmp/src", "tar --xz"),
        ("tar --exclude=foo -cf /tmp/out.tar /tmp/src", "tar --exclude="),
    ],
)
def test_bash_tar_long_form_flags_do_not_false_fire(
    command: str,
    note: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long-form tar flags (``--xattrs``, ``--xz``, ``--exclude=*``) are NOT extracts.

    Reviewer v3 non-blocking regression: the previous greedy
    ``startswith("-x")`` heuristic treated ``--xattrs`` and ``--xz``
    as extract modes and produced fail-closed false-positives. The
    v3 ``_is_tar_extract`` helper now requires a single-dash cluster
    (with ``x`` in the cluster) or the long form ``--extract``.

    Targets under ``/tmp/`` are outside any role's allow-list, but
    the hook should not surface them as paths at all when the tar
    invocation is in create mode — so ``decide()`` returns no
    decision (allow-through, since the path list is empty).
    """
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_REPO_ROOT", "/home/egg/repos/egg")
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    result = hook_entry_mod.decide(payload)
    assert result == {} or "decision" not in result, (
        f"{note}: long-form tar flag must not trip extract-mode false-positive; "
        f"command={command!r} → {result!r}"
    )


def test_bash_redirect_inside_outer_quote_does_not_emit_phantom_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer v3 non-blocking: stray-quote artifacts don't leak.

    For ``bash -c 'echo x > /restricted/file'`` the ``_REDIRECT_RE``
    first-pass on the raw outer command captures ``/restricted/file'``
    (with the trailing single-quote) while the recursive ``bash -c``
    handler captures ``/restricted/file`` cleanly. Without the
    unmatched-quote filter the policy checker would see both paths and
    could fail-closed defensively on the phantom. The v3 filter drops
    candidates with an unmatched ``'`` / ``"`` from the regex pass; the
    recursive handler still surfaces the clean path.
    """
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_REPO_ROOT", "/home/egg/repos/egg")
    command = "bash -c 'echo x > /home/egg/repos/egg/orchestrator/concurrent_executor.py'"
    paths, ambiguous = hook_entry_mod._bash_write_paths(command)
    # The recursive handler must surface the inner path exactly once
    # — no phantom-with-trailing-quote duplicate.
    clean = "/home/egg/repos/egg/orchestrator/concurrent_executor.py"
    assert paths.count(clean) == 1, (
        f"clean inner path must appear exactly once; got paths={paths!r}"
    )
    assert not any(p.endswith("'") or p.endswith('"') for p in paths), (
        f"no path may carry a stray quote artefact; got paths={paths!r}"
    )
    # The clean inner path is the only one we expect.
    assert paths == [clean], f"expected exactly [{clean!r}]; got {paths!r}"
    assert ambiguous is False


@pytest.mark.parametrize(
    "command,note",
    [
        ("cp x.txt $DEST", "shell var dest"),
        ("echo hi > `pwd`/file.py", "backtick dest"),
        ('python3 -c "open(\\"x.py\\", \\"w\\").write(\\"hi\\")"', "python -c"),
    ],
)
def test_bash_ambiguous_command_fails_closed(
    command: str,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous Bash commands (shell expansion, python -c) fail closed."""
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    result = hook_entry_mod.decide(payload)
    assert result.get("decision") == "block", (
        f"{note}: ambiguous Bash command must fail closed; command={command!r} → {result!r}"
    )


def test_bash_read_only_command_allows_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bash read-only commands (ls, cat, grep) pass the hook."""
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la && cat README.md | grep egg"},
    }
    result = hook_entry_mod.decide(payload)
    assert result == {} or "decision" not in result, f"Read-only Bash must pass: {result!r}"


# ---------------------------------------------------------------------------
# v2 security NACK #3: malformed JSON stdin fails CLOSED
# ---------------------------------------------------------------------------


def test_hook_entry_script_fails_closed_on_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed JSON on stdin returns ``decision=block``."""
    hook_entry = Path("/home/egg/repos/egg/orchestrator/substrate/claude_code/hook_entry.py")
    if not hook_entry.exists():
        pytest.skip(f"{hook_entry} not present")

    env = dict(os.environ)
    env["EGG_AGENT_ROLE"] = "tester"
    env["EGG_REPO_ROOT"] = "/home/egg/repos/egg"
    env["PYTHONPATH"] = (
        "/home/egg/repos/egg/shared"
        + os.pathsep
        + "/home/egg/repos/egg/orchestrator"
        + os.pathsep
        + "/home/egg/repos/egg"
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [sys.executable, str(hook_entry)],
        input="{this is not json",
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    out = json.loads(proc.stdout)
    assert out.get("decision") == "block", (
        f"Malformed JSON stdin must fail CLOSED (security NACK #3); got stdout={proc.stdout!r}"
    )
