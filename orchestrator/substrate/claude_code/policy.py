"""``PreToolUseHookPolicy`` for the Claude Code substrate (#2623).

Implements ``PolicyEnforcer`` (HITL decision cq-6: PreToolUse hooks):
a ``.claude/settings.json``-registered hook script intercepts
Write/Edit/Bash calls before they execute and denies anything that
would land outside the caller's role's allow-list. Allow/deny
semantics match
``gateway/phase_filter.py:1061 check_agent_restrictions`` because both
paths delegate to
``shared/egg_restrictions/checker.py::check_agent_file_access``.

See ``docs/architecture/claude-code-substrate.md`` for the ADR.

INTERFACE STABILITY: v0.x unstable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import hook_entry


class PreToolUseHookPolicy:
    """``PolicyEnforcer`` backed by a Claude Code PreToolUse hook.

    Two operations are exposed:

    1. ``check_write(role, path)`` — in-process check that delegates
       to the same ``check_agent_file_access`` function the hook
       script calls. Useful when the orchestrator wants to validate a
       proposed write without round-tripping through Claude Code.
    2. ``install(target_dir)`` — write a ``.claude/settings.json``
       file at ``target_dir`` that wires up the hook entry script.
       Idempotent: re-running with an existing settings.json merges
       the egg hook in alongside any user-defined hooks.
    """

    #: Template settings.json shipped alongside this module — the
    #: structural reference for what ``install()`` writes.
    SETTINGS_TEMPLATE_PATH = Path(__file__).parent / "settings.template.json"

    def __init__(self) -> None:
        pass

    def check_write(self, role: str, path: str) -> tuple[bool, str | None]:
        """Return ``(allowed, denial_message)`` for ``role`` writing ``path``.

        Args:
            role: Agent role making the write.
            path: Repo-relative path.

        Returns:
            ``(True, None)`` when allowed.
            ``(False, message)`` when denied; the message matches the
            format used by
            ``gateway/phase_filter.py:1061 check_agent_restrictions``.
        """
        from egg_restrictions.checker import check_agent_file_access

        if not role:
            return (True, None)
        allowed, blocked, reason = check_agent_file_access(role, [path], repo=None)
        if allowed:
            return (True, None)
        return (False, reason)

    def install(self, target_dir: str | Path) -> Path:
        """Write ``.claude/settings.json`` under ``target_dir``.

        Args:
            target_dir: The directory whose ``.claude/`` subdir
                should receive the settings file. Must be either the
                user's ``$HOME`` or a path under it (typically the
                repo root the user is running egg in). System paths
                like ``/etc/`` or ``/`` are rejected as a defense in
                depth against a caller-supplied ``target_dir`` —
                reviewer_security v1 non-blocking #4.

        Returns:
            The path that was written.

        Raises:
            ValueError: If ``target_dir`` is not under the user's
                ``$HOME`` (or ``$HOME`` cannot be resolved).
        """
        root = Path(target_dir).expanduser().resolve()
        home_env = os.environ.get("HOME")
        if not home_env:
            raise ValueError(
                "PreToolUseHookPolicy.install: $HOME is unset; refusing "
                "to write .claude/settings.json to an unknown location."
            )
        home_resolved = Path(home_env).resolve()
        try:
            if root != home_resolved and not root.is_relative_to(home_resolved):
                raise ValueError(
                    f"PreToolUseHookPolicy.install: target_dir {root} is "
                    f"not under $HOME ({home_resolved}); refusing to "
                    f"write .claude/settings.json. Path-escape guard."
                )
        except AttributeError:  # pragma: no cover
            # Python <3.9 fallback — never hit on our 3.11+ runtime.
            if not str(root).startswith(str(home_resolved) + os.sep) and root != home_resolved:
                raise ValueError(
                    f"PreToolUseHookPolicy.install: target_dir {root} is "
                    f"not under $HOME ({home_resolved})."
                ) from None
        out_dir = root / ".claude"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "settings.json"
        template = json.loads(self.SETTINGS_TEMPLATE_PATH.read_text())

        # Merge with any pre-existing settings.json so we don't blow
        # away user hooks. Egg's hook gets appended (idempotent: skip
        # if already present).
        #
        # Reviewer_code v2 blocker #2: on JSON decode error of the
        # existing settings.json we FAIL LOUD instead of silently
        # using ``{}``. The previous behavior silently replaced the
        # user's settings (including unrelated hooks / statusline /
        # plugin enablement) with the egg-substrate template. We now
        # raise ``ValueError`` with a clear message naming the path
        # so the operator can fix the typo themselves.
        existing: dict
        if out_path.exists():
            try:
                raw = out_path.read_text()
            except OSError as exc:
                raise ValueError(
                    f"PreToolUseHookPolicy.install: existing "
                    f"settings.json at {out_path} could not be read: "
                    f"{exc}"
                ) from exc
            if raw.strip():
                try:
                    existing = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"PreToolUseHookPolicy.install: existing "
                        f"settings.json at {out_path} is not valid "
                        f"JSON ({exc.msg} at line {exc.lineno} col "
                        f"{exc.colno}). Refusing to overwrite — fix "
                        f"the file manually (or move it aside) and "
                        f"re-run install. This avoids silently "
                        f"obliterating the user's prior hooks / "
                        f"statusline / plugin enablement."
                    ) from exc
                if not isinstance(existing, dict):
                    raise ValueError(
                        f"PreToolUseHookPolicy.install: existing "
                        f"settings.json at {out_path} is not a JSON "
                        f"object (top-level type: "
                        f"{type(existing).__name__}). Refusing to "
                        f"overwrite."
                    )
            else:
                existing = {}
        else:
            existing = {}

        merged = self._merge_hooks(existing, template)
        out_path.write_text(json.dumps(merged, indent=2) + "\n")
        return out_path

    @staticmethod
    def _merge_hooks(existing: dict, template: dict) -> dict:
        """Merge ``template`` into ``existing``, deduping egg hooks."""
        merged = dict(existing)
        merged_hooks = dict(existing.get("hooks", {}))
        template_hooks = template.get("hooks", {})
        for hook_kind, entries in template_hooks.items():
            current = list(merged_hooks.get(hook_kind, []))
            for entry in entries:
                # An egg hook is identified by referencing the hook
                # entry module path.
                if entry not in current:
                    current.append(entry)
            merged_hooks[hook_kind] = current
        merged["hooks"] = merged_hooks
        return merged

    # Re-export the underlying ``decide`` function so unit tests can
    # synthesize hook stdin payloads and assert decisions without
    # spawning a subprocess.
    decide = staticmethod(hook_entry.decide)
