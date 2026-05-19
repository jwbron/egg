#!/usr/bin/env python3
"""PreToolUse hook entry script for the Claude Code substrate (#2623).

Wired up by ``.claude/settings.json`` (template at
``orchestrator/substrate/claude_code/settings.template.json``):

.. code-block:: json

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
            "hooks": [
              {
                "type": "command",
                "command": "python3 -m orchestrator.substrate.claude_code.hook_entry"
              }
            ]
          }
        ]
      }
    }

Claude Code invokes the hook before any matching tool runs. The hook
receives a JSON object on stdin with the tool name and tool input
(see the official PreToolUse hook spec); it returns a JSON object on
stdout. To deny the call, emit::

    {
      "decision": "block",
      "reason": "<human-readable explanation>"
    }

To allow it, emit ``{}`` (or any object without ``decision``).

Threat model — first-tier enforcement only
-------------------------------------------

In the claude-code substrate the gateway sidecar is gone by
construction (cq-6). This hook is the **first-tier** enforcement
layer for substrate-managed write prefixes (``.egg-state/``,
``.claude/``, ``.github/``, ``shared/egg_restrictions/``). Per the
ADR's R2 deferral, the design's full enforcement story rides on a
**second-tier MCP-validator** path that re-checks role / phase /
prefix at the MCP-tool boundary. This hook intentionally does NOT
attempt to be the only enforcement layer — Bash is a quoting-
unbounded surface, and pretending the regex-and-shlex parser
herein is a complete sandbox would be a security claim the
implementation cannot back. Treat this hook as a coarse, cheap
filter that catches the common explicit-write shapes; the MCP
validator (or the role-confined harness that ships the second
substrate role) catches the cases this hook necessarily misses.

What this hook DOES catch (reviewer v1 blocker #4 — broadened set):

1. ``Bash`` write-shaped verbs the parser explicitly handles:
   - Redirection (``>``, ``>>``, ``&>``, ``2>``, ``2>>``, etc.) —
     fail-closed on ``$(...)`` / backtick destinations.
   - File-mover / file-mutator verbs: ``cp``, ``mv``, ``install``,
     ``rsync``, ``tee``, ``dd of=``, ``sed -i``, ``ln``/``link``,
     ``rm``, ``chmod``, ``chown``, ``truncate``, ``awk -i inplace``,
     ``perl -i``.
   - Network-fetch-to-disk verbs: ``wget -O``, ``curl -o``,
     ``curl --output``, ``curl --output-dir``.
   - Git mutation verbs that write working-tree paths:
     ``git mv``, ``git rm``, ``git apply``, ``git checkout -- <p>``,
     ``git restore <p>``.
   - Archive-extraction verbs: ``tar -x``, ``unzip``.
   - Shell-of-shell forms: ``bash -c '...'``, ``sh -c '...'``,
     ``zsh -c '...'`` recursively parse the inner command.
   - ``python``/``python3 -c`` containing common write signatures.

2. Ambiguous shape → fail-closed: ``$(...)`` / backtick in a
   destination, malformed ``shlex`` input, unknown verb whose
   tokens include a ``>`` / ``>>`` redirect inside quotes the
   ``_REDIRECT_RE`` already flagged.

3. ``Path(p).resolve()`` symlink-aware comparison (security finding
   #2), ``JSONDecodeError`` fail-closed (security finding #3),
   role-missing fail-closed inside substrate-managed prefixes
   (security finding #4).

What this hook does NOT catch (deliberate, with mitigation):

- Process substitution (``cat >(tee /restricted)``), heredoc
  redirects (``cat <<EOF > /restricted``), and fd-duplication
  tricks (``exec 3>/restricted``). These require a Bash-grammar
  parser; the MCP validator's role-confined tool surface is the
  intended catch.
- Arbitrary ``python -c`` bodies that write without using the
  obvious ``open(...)`` / ``Path(...)`` markers. The MCP
  validator's role-confined tool surface is again the intended
  catch; this hook's Python-string heuristic is a coarse filter.
- Allowing ``python3 -m orchestrator.substrate.*`` broadly. The
  follow-up issue tightens this to a per-entrypoint allowlist.

The hook delegates allow/deny decisions for paths it DOES extract
to ``shared/egg_restrictions/checker.py::check_agent_file_access``
— the same symbol ``gateway/phase_filter.py:1061
check_agent_restrictions`` uses. There is no parallel restriction
logic.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

#: Prefixes whose writes must always be policed even when
#: ``EGG_AGENT_ROLE`` is unset. The user's plain Claude Code session
#: outside these prefixes keeps writing freely; only substrate-
#: managed locations engage the fail-closed default.
_RESTRICTED_PREFIXES: tuple[str, ...] = (
    ".egg-state/",
    ".claude/",
    ".github/",
    "shared/egg_restrictions/",
)


def _extract_file_tool_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Return the set of paths a Write/Edit-family tool would write."""
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(path, str) and path:
            return [path]
        return []
    if tool_name == "MultiEdit":
        path = tool_input.get("file_path")
        if isinstance(path, str) and path:
            return [path]
        return []
    return []


#: Regex for redirection-style write targets in a Bash command. Matches
#: ``>``, ``>>``, ``&>``, ``&>>``, ``2>``, ``2>>``, ``1>``, ``1>>`` and
#: similar fd-anchored forms; the path is the next shell token.
_REDIRECT_RE = re.compile(r"(?:^|[\s;|&])(?:[12]?&?>>?|>&?)\s*([^\s;|&]+)")


def _bash_write_paths(command: str) -> tuple[list[str], bool]:
    """Best-effort extraction of write targets from a Bash command.

    Returns ``(paths, ambiguous)`` where ``ambiguous`` is True when
    the parser found a write-shaped construct it could not classify
    (e.g. a ``$(...)`` substitution in a destination position). The
    caller MUST fail closed when ``ambiguous`` is True — the hook's
    contract is "block by default whenever the substrate's
    enforcement layer cannot prove a Bash command is safe".

    The parser is intentionally conservative: it errs on the side of
    flagging paths so the policy check runs against them. A clean
    Bash command (read-only ``ls``, ``cat``, ``grep``) produces an
    empty path list and ``ambiguous=False``, allowing the call.
    """
    paths: list[str] = []
    ambiguous = False
    if not command:
        return paths, False

    # 1. Redirection writes (>, >>, &>, 2>, etc.)
    #
    # Reviewer v3 non-blocking: the raw-command first-pass can catch
    # tokens that sit inside a quoted region of an outer shell-of-shell
    # form (e.g. ``bash -c 'echo x > /restricted/file'`` captures
    # ``/restricted/file'`` — note the trailing quote). The recursive
    # handler at the ``bash -c`` branch below re-extracts the inner
    # command cleanly, so the phantom-with-quote token would land
    # alongside the clean path and produce noisy duplicate paths.
    # Filter out any candidate that contains an unmatched ``'`` or
    # ``"`` before handing it to the policy checker.
    for m in _REDIRECT_RE.finditer(command):
        target = m.group(1)
        if "$" in target or "`" in target:
            ambiguous = True
        elif target.count("'") % 2 != 0 or target.count('"') % 2 != 0:
            # Phantom path artefact from the regex catching a token
            # inside a quoted region of a longer command; the recursive
            # bash handler below picks up the clean path.
            continue
        else:
            paths.append(target)

    # 2. Token-driven write commands. Use shlex so quoted paths
    # survive intact. shlex on malformed input raises ValueError;
    # treat that as "ambiguous → fail closed".
    try:
        tokens = shlex.split(command, comments=True, posix=True)
    except ValueError:
        return paths, True

    # Walk the token list, classifying write-shaped invocations.
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = os.path.basename(tok)
        if base in {"cp", "mv", "install", "rsync"}:
            # The last positional non-flag arg is the destination.
            args = [t for t in tokens[i + 1 :] if not t.startswith("-")]
            if args:
                dest = args[-1]
                if "$" in dest or "`" in dest:
                    ambiguous = True
                else:
                    paths.append(dest)
            break
        if base == "tee":
            for arg in tokens[i + 1 :]:
                if arg.startswith("-"):
                    continue
                if "$" in arg or "`" in arg:
                    ambiguous = True
                else:
                    paths.append(arg)
            break
        if base == "dd":
            for arg in tokens[i + 1 :]:
                if arg.startswith("of="):
                    dest = arg[3:]
                    if "$" in dest or "`" in dest:
                        ambiguous = True
                    else:
                        paths.append(dest)
            break
        if base == "sed":
            # sed -i / --in-place mutates the named file(s).
            in_place = any(
                t == "-i" or t.startswith("-i") or t == "--in-place" for t in tokens[i + 1 :]
            )
            if in_place:
                for t in tokens[i + 1 :]:
                    if t.startswith("-"):
                        continue
                    if "$" in t or "`" in t:
                        ambiguous = True
                    else:
                        paths.append(t)
            break
        if base in {"ln", "link"}:
            non_flags = [t for t in tokens[i + 1 :] if not t.startswith("-")]
            if non_flags:
                link_path = non_flags[-1]
                if "$" in link_path or "`" in link_path:
                    ambiguous = True
                else:
                    paths.append(link_path)
            break
        # Reviewer v1 blocker #4: ``rm``, ``chmod``, ``chown``,
        # ``truncate``, ``awk -i inplace``, ``perl -i``. Each
        # mutates the listed paths in place; surface them all to
        # the policy checker.
        if base in {"rm", "chmod", "chown", "truncate"}:
            for arg in tokens[i + 1 :]:
                if arg.startswith("-"):
                    continue
                if "$" in arg or "`" in arg:
                    ambiguous = True
                else:
                    paths.append(arg)
            break
        if base == "awk":
            rest = tokens[i + 1 :]
            in_place = any(
                t == "-i" or (t.startswith("-i") and "inplace" in t.lower()) or "inplace" in t
                for t in rest
            )
            if in_place:
                # Last token is conventionally the file in `awk -i
                # inplace 'script' file`.
                non_flags = [t for t in rest if not t.startswith("-")]
                if non_flags:
                    candidate = non_flags[-1]
                    if "$" in candidate or "`" in candidate:
                        ambiguous = True
                    else:
                        paths.append(candidate)
            break
        if base == "perl":
            rest = tokens[i + 1 :]
            in_place = any(t == "-i" or t.startswith("-i") for t in rest)
            if in_place:
                non_flags = [t for t in rest if not t.startswith("-")]
                # `perl -i.bak -pe '<expr>' file` — the script is
                # one of the non-flag args; we conservatively
                # surface every non-flag candidate to the policy
                # checker. Spurious hits land on the script string
                # ("'<expr>'"), which the policy checker treats as
                # an unknown path and ignores per its own resolve
                # logic.
                for cand in non_flags:
                    if "$" in cand or "`" in cand:
                        ambiguous = True
                    else:
                        paths.append(cand)
            break
        # Network-fetch-to-disk verbs.
        if base in {"wget", "curl"}:
            rest = tokens[i + 1 :]
            j = 0
            while j < len(rest):
                arg = rest[j]
                if arg in {"-O", "-o", "--output"} and j + 1 < len(rest):
                    dest = rest[j + 1]
                    if "$" in dest or "`" in dest:
                        ambiguous = True
                    else:
                        paths.append(dest)
                    j += 2
                    continue
                if arg.startswith("--output-dir") or arg == "--output-document":
                    if "=" in arg:
                        dest = arg.split("=", 1)[1]
                    elif j + 1 < len(rest):
                        dest = rest[j + 1]
                        j += 1
                    else:
                        dest = ""
                    if dest:
                        if "$" in dest or "`" in dest:
                            ambiguous = True
                        else:
                            paths.append(dest)
                j += 1
            break
        # Archive-extraction verbs. Both can land arbitrary paths
        # in the working tree; flag the explicit -C target when
        # present, otherwise mark the whole command ambiguous so
        # the policy checker engages against an obviously-unsafe
        # shape.
        if base == "tar":
            rest = tokens[i + 1 :]

            # Reviewer v2 non-blocking: narrow the extract-mode
            # detection to the actual tar extract flags. Previously
            # any token starting with ``-x`` (e.g. ``--xattrs``,
            # ``--xz``) was treated as an extract, producing
            # fail-closed false-positives. Match the named extract
            # forms exactly (long ``--extract`` + short modes that
            # encode ``x``: ``-x`` alone or short-flag clusters like
            # ``-xf``, ``-xzf``, ``-xjf``, ``-xJf``, ``-xvf``).
            def _is_tar_extract(tok: str) -> bool:
                if tok == "--extract":
                    return True
                if not tok.startswith("-") or tok.startswith("--"):
                    return False
                # Single-dash cluster: each char after the leading
                # dash is a short flag; ``x`` anywhere in the cluster
                # means extract.
                return "x" in tok[1:]

            extracts = any(_is_tar_extract(t) for t in rest)
            if extracts:
                target_dir: str | None = None
                for k, t in enumerate(rest):
                    if t == "-C" and k + 1 < len(rest):
                        target_dir = rest[k + 1]
                if target_dir is None:
                    ambiguous = True
                else:
                    if "$" in target_dir or "`" in target_dir:
                        ambiguous = True
                    else:
                        paths.append(target_dir)
            break
        if base == "unzip":
            rest = tokens[i + 1 :]
            target_dir = None
            for k, t in enumerate(rest):
                if t == "-d" and k + 1 < len(rest):
                    target_dir = rest[k + 1]
            if target_dir is None:
                ambiguous = True
            else:
                if "$" in target_dir or "`" in target_dir:
                    ambiguous = True
                else:
                    paths.append(target_dir)
            break
        # ``git`` write subcommands. The leaf paths are the trailing
        # non-flag args; we surface them all.
        if base == "git" and i + 1 < len(tokens):
            sub = tokens[i + 1]
            if sub in {"mv", "rm", "apply", "checkout", "restore"}:
                rest = tokens[i + 2 :]
                for arg in rest:
                    if arg.startswith("-"):
                        continue
                    if arg == "--":
                        continue
                    if "$" in arg or "`" in arg:
                        ambiguous = True
                    else:
                        paths.append(arg)
                break
        # Shell-of-shell forms: recurse into the inner command so
        # ``bash -c 'echo x > /restricted'`` is parsed, not silently
        # allowed (reviewer v1 blocker #4). Reviewer v2 non-blocking:
        # also handle combined short-flag clusters like ``bash -lc
        # '...'`` or ``bash -xc '...'`` where the ``c`` rides along
        # with other single-char options.
        if base in {"bash", "sh", "zsh", "dash", "ksh"}:
            rest = tokens[i + 1 :]
            c_index: int | None = None
            for k, tok in enumerate(rest):
                if tok == "-c":
                    c_index = k
                    break
                # Combined short cluster: starts with single dash, no
                # second dash, and includes ``c`` somewhere in the
                # cluster. Matches ``-lc``, ``-xc``, ``-cx``, etc.
                if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
                    c_index = k
                    break
            if c_index is not None and c_index + 1 < len(rest):
                inner = rest[c_index + 1]
                inner_paths, inner_ambiguous = _bash_write_paths(inner)
                paths.extend(inner_paths)
                if inner_ambiguous:
                    ambiguous = True
            break
        if base == "python3" or base == "python":
            # python -c "..." is arbitrary Python — flag as ambiguous
            # unless the command obviously doesn't write
            # (we don't try to parse Python here).
            rest = tokens[i + 1 :]
            if "-c" in rest:
                idx = rest.index("-c")
                if idx + 1 < len(rest):
                    body = rest[idx + 1]
                    if any(
                        sig in body
                        for sig in (
                            "open(",
                            "os.write",
                            "Path(",
                            "shutil",
                            "with open",
                        )
                    ):
                        ambiguous = True
            # Tightened allow-list (reviewer v1 non-blocking): only
            # the named substrate entrypoints get a free pass, not
            # any module under ``orchestrator.substrate.*``.
            if "-m" in rest:
                idx = rest.index("-m")
                if idx + 1 < len(rest):
                    mod = rest[idx + 1]
                    _ALLOWED_PY_M_MODS = ("orchestrator.substrate.claude_code.hook_entry",)
                    if mod in _ALLOWED_PY_M_MODS:
                        i += 1
                        continue
            break
        i += 1

    return paths, ambiguous


def _extract_write_paths(tool_name: str, tool_input: dict[str, Any]) -> tuple[list[str], bool]:
    """Return ``(paths, ambiguous)`` for any write-side tool.

    For file-side tools (``Write``/``Edit``/etc.) ``ambiguous`` is
    always False — the input carries a single explicit path.
    For ``Bash``, the parser may flag the command as ambiguous, in
    which case the caller fails closed.
    """
    if tool_name == "Bash":
        command = tool_input.get("command") or ""
        if not isinstance(command, str):
            return [], True
        return _bash_write_paths(command)
    return _extract_file_tool_paths(tool_name, tool_input), False


def _repo_relative(path: str, repo_root: str | None) -> str:
    """Canonicalise ``path`` to a repo-relative key.

    SECURITY: uses ``Path(p).resolve()`` (security finding #2) so
    symlinks are followed to their targets BEFORE the prefix
    comparison. The gateway's defense at
    ``gateway/worktree_manager.py:1700-1711`` follows the same
    pattern: resolve first, then compare. Without this, a symlink
    inside an allow-listed directory pointing outside the workspace
    laundered the write past the allow-list.

    When the resolved path is outside the repo root, return the
    resolved path verbatim. The caller treats any path that resolves
    outside the repo root as a write the substrate must deny.
    """
    if not path:
        return path
    try:
        resolved = Path(path).resolve()
    except OSError:
        # Path does not exist yet; fall back to a string normalize
        # so we still get a deterministic key for the pattern check.
        # The pattern matcher's blocklist will catch obviously-bad
        # paths (``..`` traversal) via its own normalization.
        return os.path.normpath(path)
    if not repo_root:
        return str(resolved)
    try:
        repo_root_resolved = Path(repo_root).resolve()
    except OSError:
        return str(resolved)
    try:
        rel = resolved.relative_to(repo_root_resolved)
    except ValueError:
        # Outside repo root.
        return str(resolved)
    return str(rel)


def _is_inside_restricted_prefix(path: str) -> bool:
    """Return True if ``path`` lands under a substrate-managed prefix.

    Used to gate the fail-closed default when ``EGG_AGENT_ROLE`` is
    unset: writes outside any restricted prefix continue to fail-open
    (keeps the user's plain Claude Code session unaffected by hook
    installation); writes inside fail closed.
    """
    if not path:
        return False
    normalized = os.path.normpath(path).lstrip("/")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in _RESTRICTED_PREFIXES
    )


def _path_resolves_inside_repo(path: str, repo_root: str | None) -> bool:
    """Return True iff the path resolves to a location inside repo_root.

    Used as a final symlink-aware guard: even if the repo-relative
    string starts with an allow-listed prefix, a symlink at that
    location may redirect the actual write outside repo_root.
    """
    if not path:
        return False
    if not repo_root:
        # Without a repo root we cannot prove the write is in scope.
        return False
    try:
        resolved = Path(path).resolve()
        root_resolved = Path(repo_root).resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(root_resolved)
        return True
    except ValueError:
        return False


def decide(stdin_blob: dict[str, Any]) -> dict[str, Any]:
    """Compute the hook decision for a single PreToolUse invocation.

    Args:
        stdin_blob: The parsed JSON object Claude Code wrote to the
            hook's stdin.

    Returns:
        A dict to be JSON-serialized as the hook stdout. Empty or
        missing ``decision`` field means "allow"; ``decision="block"``
        with a ``reason`` blocks the call.
    """
    tool_name = stdin_blob.get("tool_name") or stdin_blob.get("tool", "")
    tool_input = stdin_blob.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    paths, ambiguous = _extract_write_paths(tool_name, tool_input)

    # Ambiguous Bash → fail closed (security finding #1).
    if ambiguous:
        return {
            "decision": "block",
            "reason": (
                f"egg PreToolUseHookPolicy: cannot statically verify "
                f"{tool_name} write targets (shell expansion / "
                f"unparseable command). Rewrite the command so the "
                f"write target is a literal path, or disable the hook "
                f"if you intend to opt out of substrate enforcement."
            ),
        }

    if not paths:
        return {}

    role = _resolve_active_role()
    repo_root = os.environ.get("EGG_REPO_ROOT") or os.environ.get("EGG_WORKTREE_ROOT")

    # Symlink-aware path resolution (security finding #2).
    repo_paths = [_repo_relative(p, repo_root) for p in paths]

    # Defense in depth (security finding #2 continued): even if the
    # resolved repo-relative path looks fine, the original path may
    # have escaped via a symlink. Verify the original path resolves
    # inside the repo when a repo_root is configured.
    if repo_root:
        for original in paths:
            if not _path_resolves_inside_repo(original, repo_root):
                # Original path resolves outside repo root — deny.
                return {
                    "decision": "block",
                    "reason": (
                        f"egg PreToolUseHookPolicy denied {tool_name}: "
                        f"path {original!r} resolves outside the repo "
                        f"root {repo_root}. Symlink-laundered writes "
                        f"are rejected by design (security finding #2)."
                    ),
                }

    if not role:
        # Security finding #4: fail closed for substrate-managed
        # prefixes even when the role env is missing. The plain
        # Claude Code session (writes outside .egg-state/ etc.)
        # continues to work — the fail-closed default only engages
        # when a write lands in a prefix the substrate owns.
        for p in repo_paths:
            if _is_inside_restricted_prefix(p):
                return {
                    "decision": "block",
                    "reason": (
                        f"egg PreToolUseHookPolicy denied {tool_name}: "
                        f"EGG_AGENT_ROLE is unset but the write target "
                        f"{p!r} is inside a substrate-managed prefix. "
                        f"Either set EGG_AGENT_ROLE or write outside "
                        f"the .egg-state / .claude / .github / "
                        f"shared/egg_restrictions/ trees."
                    ),
                }
        return {}

    # Defense-in-depth literal-criterion satisfier (task-1-4
    # acceptance bullet 5 + reviewer_code_holistic finding #5):
    # touch ``build_agent_patterns`` directly to verify the role is
    # registered. ``check_agent_file_access`` already calls into the
    # same patterns module transitively, but reviewers wanted the
    # direct import in the call graph.
    try:
        from egg_restrictions.patterns import build_agent_patterns

        _patterns = build_agent_patterns(repo=None)
        if role not in _patterns:
            return {
                "decision": "block",
                "reason": (
                    f"egg PreToolUseHookPolicy: role '{role}' has no "
                    f"registered pattern in shared/egg_restrictions/"
                    f"patterns.py:build_agent_patterns. Deny-by-default."
                ),
            }
    except ImportError:
        # build_agent_patterns unavailable — fall through to the
        # higher-level check which has its own deny-by-default.
        pass

    from egg_restrictions.checker import check_agent_file_access

    allowed, blocked, reason = check_agent_file_access(role, repo_paths, repo=None)
    if allowed:
        return {}

    return {
        "decision": "block",
        "reason": (f"egg PreToolUseHookPolicy denied {tool_name} for role '{role}': {reason}"),
    }


def _resolve_active_role() -> str:
    """Resolve the agent role for the current tool call.

    Reads ``EGG_AGENT_ROLE`` from the env first. When the env var
    is unset, falls back to the sentinel file the spawner writes at
    ``$HOME/.claude/egg-active-role.json`` (reviewer_code_holistic v1
    finding #8 — spawn↔hook role coordination across process
    boundaries).

    Reviewer_code v2 blocker #1: the sentinel is PID-stamped by the
    spawner. When the recorded PID is no longer alive (crashed
    pipeline, OOM-kill, hard kill), the sentinel is treated as
    missing — preventing a stale sentinel from impeding the user's
    next plain Claude Code session. Live-PID sentinels still
    function normally as a fallback when env propagation is dropped
    in nested dispatch.

    Returns an empty string when neither env nor a live-PID sentinel
    resolves a role.
    """
    role = os.environ.get("EGG_AGENT_ROLE", "").strip()
    if role:
        return role
    home = os.environ.get("HOME")
    if not home:
        return ""
    sentinel = Path(home) / ".claude" / "egg-active-role.json"
    try:
        if sentinel.exists():
            blob = json.loads(sentinel.read_text())
            sentinel_role = blob.get("role")
            sentinel_pid = blob.get("pid")
            if not isinstance(sentinel_role, str) or not sentinel_role.strip():
                return ""
            # PID-liveness check (reviewer_code v2 blocker #1): treat
            # sentinel as missing when the owning orchestrator
            # process is no longer running. This prevents a stale
            # sentinel from a crashed pipeline from impeding the
            # user's next plain Claude Code session.
            if isinstance(sentinel_pid, int) and sentinel_pid > 0:
                try:
                    os.kill(sentinel_pid, 0)
                except (ProcessLookupError, PermissionError):  # fmt: skip
                    # ProcessLookupError: PID is gone entirely.
                    # PermissionError: PID is alive but owned by a
                    # different user — the orchestrator's spawner
                    # must own the process for role-routing to make
                    # sense, so we still treat the sentinel as stale
                    # in this case (fail-safe default for the user's
                    # plain Claude Code session).
                    return ""
                except OSError:
                    # Unknown errno — we cannot classify, so we
                    # fall through and trust the sentinel here
                    # (rather than denying writes) to avoid kernel
                    # quirks locking the user out of their own
                    # session. The blast radius is bounded: the
                    # operator can manually delete
                    # ``$HOME/.claude/egg-active-role.json`` if
                    # this branch ever misfires.
                    pass
            # Either no PID stamp (legacy sentinel) or PID is alive.
            return sentinel_role.strip()
    except (json.JSONDecodeError, OSError):  # fmt: skip
        pass
    return ""


def main() -> int:
    """Read JSON from stdin, write decision JSON to stdout."""
    try:
        raw = sys.stdin.read()
        blob = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        # Security finding #3: fail CLOSED. Even as a first-tier
        # filter (per the module-top "first-tier enforcement only"
        # threat-model rewrite), a fail-open on a malformed stdin is
        # a complete-bypass primitive an attacker who can shape any
        # tool input can trigger — every write would slip past this
        # tier with no signal handed to the second-tier MCP
        # validator. Failing closed keeps the filter cheap and
        # honest about its scope.
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": (
                        "egg PreToolUseHookPolicy: malformed hook stdin "
                        "(JSONDecodeError). Failing closed — retry the "
                        "tool call with a well-formed input."
                    ),
                }
            )
        )
        return 0

    decision = decide(blob)
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
