"""Claude Code config, agent-rules, and bashrc setup."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from ._config import Config, Logger
from ._core import chown_recursive

_CLAUDE_RULES_DIR = Path("/opt/claude-rules")


def setup_agent_rules(config: Config, logger: Logger) -> None:
    """Set up CLAUDE.md agent rules."""
    rules_dir = _CLAUDE_RULES_DIR

    # All rules always included so CLI tools are discoverable in any session
    rules_order = [
        "mission.md",
        "environment.md",
        "code-standards.md",
        "test-workflow.md",
        "pr-descriptions.md",
        "orchestrator.md",
        "contract.md",
        "checkpoint.md",
    ]

    if not (rules_dir / "mission.md").exists():
        return

    # Combine rules into ~/.claude/CLAUDE.md (user-level global config)
    # This is the documented Claude Code location for user-wide instructions,
    # loaded automatically regardless of CWD or which repo is checked out.
    claude_md = config.claude_dir / "CLAUDE.md"
    content_parts = []

    for rule_file in rules_order:
        rule_path = rules_dir / rule_file
        if rule_path.exists():
            content_parts.append(rule_path.read_text())

    # Ensure ~/.claude/ exists (setup_claude creates it later, but we need it now)
    config.claude_dir.mkdir(parents=True, exist_ok=True)
    claude_md.write_text("\n\n---\n\n".join(content_parts))
    os.chown(claude_md, config.runtime_uid, config.runtime_gid)

    # AGENTS.md is the cross-tool industry alias for CLAUDE.md; expose both
    # so non-Claude agent frontends can discover the same rules.
    agents_md = config.claude_dir / "AGENTS.md"
    if agents_md.exists() or agents_md.is_symlink():
        agents_md.unlink()
    agents_md.symlink_to(claude_md.name)

    # Clean up stale CLAUDE.md / AGENTS.md files from previous container runs.
    # _chdir_to_single_repo() re-creates symlinks at CWD later when the session
    # starts; this cleanup ensures a fresh state.
    # Covers all three CWD scenarios: home, repos_dir, and single-repo.
    #
    # Inside a repo subdir we must NOT touch a symlink that doesn't point at
    # our global rules file — a repo can legitimately commit
    # ``AGENTS.md -> CLAUDE.md`` (relative target) as a cross-tool alias, and
    # that file is tracked content we must preserve. Only unlink subdir
    # symlinks that resolve to ``~/.claude/CLAUDE.md`` (the absolute target
    # we wrote on a previous startup).
    global_target = (config.claude_dir / "CLAUDE.md").resolve()
    for name in ("CLAUDE.md", "AGENTS.md"):
        stale_home = config.user_home / name
        if stale_home.exists() or stale_home.is_symlink():
            stale_home.unlink()
        stale_repos = config.repos_dir / name
        if stale_repos.exists() or stale_repos.is_symlink():
            stale_repos.unlink()
        # Single-repo case: symlink is inside repos_dir/<repo>/<name>
        if config.repos_dir.exists():
            for subdir in config.repos_dir.iterdir():
                if subdir.is_dir() and (subdir / ".git").exists():
                    stale_repo = subdir / name
                    if stale_repo.is_symlink():
                        try:
                            if stale_repo.resolve(strict=False) == global_target:
                                stale_repo.unlink()
                        except OSError:
                            # Cycles or permission errors — leave the symlink
                            # in place rather than risk deleting tracked content.
                            pass

    logger.success("AI agent rules installed: ~/.claude/CLAUDE.md (+ AGENTS.md alias)")
    logger.info(f"  Combined {len(rules_order)} rule files (index-based per LLM Doc architecture)")
    logger.info("  Note: Reference docs at $EGG_REPO_PATH/docs/ (fetched on-demand)")


def setup_claude(config: Config, logger: Logger) -> None:
    """Set up Claude CLI configuration."""
    # Verify Claude Code CLI is installed
    claude_bin = shutil.which("claude")
    if not claude_bin:
        expected = config.user_home / ".local" / "bin" / "claude"
        logger.error(f"Claude Code CLI not found in PATH (expected at {expected})")
        logger.error("  Rebuild the sandbox image: egg --reset")
        sys.exit(1)
    logger.success(f"Claude Code CLI found: {claude_bin}")

    # Create directories
    config.claude_dir.mkdir(parents=True, exist_ok=True)
    (config.claude_dir / "commands").mkdir(exist_ok=True)
    (config.user_home / ".config" / "claude-code").mkdir(parents=True, exist_ok=True)

    # Check API key (only warn if using api_key auth method)
    # Validate auth method
    if config.anthropic_auth_method not in config.VALID_AUTH_METHODS:
        logger.warn(
            f"Invalid ANTHROPIC_AUTH_METHOD '{config.anthropic_auth_method}', "
            f"expected one of: {', '.join(config.VALID_AUTH_METHODS)}"
        )

    if config.anthropic_api_key:
        logger.success("Anthropic API key configured")
    elif config.anthropic_auth_method == "oauth" or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        logger.success("Anthropic OAuth authentication enabled")
    else:
        logger.warn("ANTHROPIC_API_KEY not set and no OAuth token found")
        logger.info("  Set via: export ANTHROPIC_API_KEY=sk-ant-...")
        logger.info("  Or use OAuth: export ANTHROPIC_AUTH_METHOD=oauth")

    # Copy custom commands
    commands_src = Path("/usr/local/share/claude-commands")
    if commands_src.exists():
        for cmd in commands_src.glob("*.md"):
            if cmd.name != "README.md":
                (config.claude_dir / "commands" / cmd.name).write_text(cmd.read_text())
        logger.success("Custom commands installed:")
        if not config.quiet:
            for cmd in (config.claude_dir / "commands").glob("*.md"):
                print(f"    @{cmd.stem}")

    # Copy skills (directory-based, each skill is a subdirectory with SKILL.md)
    skills_src = Path("/usr/local/share/claude-skills")
    if skills_src.exists():
        skills_dest = config.claude_dir / "skills"
        skills_dest.mkdir(parents=True, exist_ok=True)
        installed = []
        for skill_dir in skills_src.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                target = skills_dest / skill_dir.name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(skill_dir, target)
                installed.append(skill_dir.name)
        if installed:
            logger.success(f"Skills installed: {', '.join(installed)}")

    # Create settings.json
    settings: dict[str, Any] = {
        "defaultPermissionMode": "bypassPermissions",
        "skipDangerousModePermissionPrompt": True,
        "autoApproveEdits": True,
        "editorMode": "normal",
        "autoUpdate": False,
        "outputStyle": "default",
        "defaultModel": "opus[1m]",
        "showResumeCommand": False,
        "memory": {"enabled": False},
    }

    # In private mode, disallow web tools at the agent config level so they are
    # never sent to the API.  The gateway still strips them as defense-in-depth.
    private_mode_env = os.environ.get("EGG_PRIVATE_MODE", "").lower()
    if private_mode_env in ("true", "1"):
        settings["disallowedTools"] = ["WebFetch", "WebSearch"]
        logger.info("Private mode: disallowed WebFetch/WebSearch in agent settings")

    # PreToolUse hook: deny built-in WebSearch/WebFetch on the LiteLLM→non-Anthropic
    # path (signalled by ANTHROPIC_CUSTOM_MODEL_OPTION). The Anthropic server-tool
    # schemas get stripped by LiteLLM's drop_params, so the tools silently no-op
    # with "Did 0 searches". The hook's permissionDecisionReason points the model at
    # the DuckDuckGo MCP tools (mcp__ddg__search / mcp__ddg__fetch_content), which
    # run_agent_async registers in ClaudeAgentOptions.mcp_servers on the same path
    # (settings.json has no mcpServers key — server definitions don't live there).
    #
    # Only installed on the LiteLLM path AND only in public mode: private mode
    # already disallows the web tools (above), and the DDG MCP server runs inside
    # the sandbox and must reach duckduckgo.com directly — which the locked-down
    # private-mode proxy (empty allowlist) forbids. Public mode has direct internet,
    # so the fallback can actually function there. See
    # https://github.com/jwbron/egg/issues/2856.
    custom_model_option = os.environ.get("ANTHROPIC_CUSTOM_MODEL_OPTION", "")
    if custom_model_option and private_mode_env not in ("true", "1"):
        web_block_hook = "/opt/egg-runtime/sandbox/scripts/block-builtin-web-tools.sh"
        settings["hooks"] = {
            "PreToolUse": [
                {
                    "matcher": "WebSearch",
                    "hooks": [{"type": "command", "command": web_block_hook}],
                },
                {
                    "matcher": "WebFetch",
                    "hooks": [{"type": "command", "command": web_block_hook}],
                },
            ],
        }
        logger.info(
            "LiteLLM path: installed PreToolUse hook redirecting WebSearch/WebFetch to DDG MCP"
        )

    settings_file = config.claude_dir / "settings.json"
    settings_file.write_text(json.dumps(settings, indent=2))
    os.chown(settings_file, config.runtime_uid, config.runtime_gid)

    # Ensure ~/.claude.json has required settings to skip onboarding prompts
    # We merge with any existing settings rather than overwriting
    user_state_file = config.user_home / ".claude.json"
    required_settings: dict[str, Any] = {
        "hasCompletedOnboarding": True,
        "autoUpdates": False,
        "bypassPermissionsModeAccepted": True,
        "effortCalloutDismissed": True,
    }
    # These are only set on new files, not forced on existing ones
    default_settings: dict[str, Any] = {
        "lastOnboardingVersion": "2.0.69",
        "numStartups": 1,
        "installMethod": "api_key",
    }

    # Read existing config if present
    file_existed = user_state_file.exists()
    existing_config: dict[str, Any] = {}
    if file_existed:
        try:
            existing_config = json.loads(user_state_file.read_text())
        except json.JSONDecodeError as e:
            logger.warn(f"~/.claude.json contains invalid JSON (line {e.lineno}, col {e.colno})")
            logger.warn("  File will be recreated with default settings")
            logger.warn("  This can cause Claude Code to prompt for config reset")
            existing_config = {}
        except OSError as e:
            logger.warn(f"Could not read ~/.claude.json: {e}")
            existing_config = {}

    # Check if required settings need updating
    needs_update = False
    for key, value in required_settings.items():
        if existing_config.get(key) != value:
            needs_update = True
            existing_config[key] = value

    # Add defaults only for missing keys
    for key, value in default_settings.items():
        if key not in existing_config:
            needs_update = True
            existing_config[key] = value

    # Pre-populate per-project trust dialog acceptance for all repos
    # This prevents the "Do you trust this folder?" prompt on first launch
    try:
        if config.repos_dir.exists():
            project_trust_settings = {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
            }
            # Build list of project paths first (iterdir can raise OSError)
            project_paths = [config.repos_dir] + [
                d for d in config.repos_dir.iterdir() if d.is_dir()
            ]
            # Only modify existing_config after successful enumeration
            projects = existing_config.setdefault("projects", {})
            for project_path in project_paths:
                project_key = str(project_path)
                project = projects.setdefault(project_key, {})
                for key, value in project_trust_settings.items():
                    if project.get(key) != value:
                        needs_update = True
                        project[key] = value
    except OSError:
        pass  # Don't let trust pre-population failures block config setup

    # Write back if changes needed (using atomic write to prevent corruption)
    if needs_update:
        # Write to temp file first, then atomically replace
        # This prevents partial writes if the process is interrupted
        fd, temp_path = tempfile.mkstemp(
            dir=str(user_state_file.parent),
            prefix=".claude.json.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(existing_config, f, indent=2)
            os.chown(temp_path, config.runtime_uid, config.runtime_gid)
            os.chmod(temp_path, 0o600)
            try:
                os.replace(temp_path, user_state_file)  # Atomic on POSIX
            except OSError as e:
                if e.errno == errno.EBUSY:
                    # File is bind-mounted from host - can't atomically replace
                    # Fall back to direct write (still safe: we validated JSON above)
                    logger.warn("~/.claude.json is bind-mounted, using direct write")
                    shutil.copy2(temp_path, user_state_file)
                    os.unlink(temp_path)
                else:
                    raise
        except Exception:
            # Clean up temp file on failure
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise
        user_state_status = "created" if not file_existed else "updated"
    else:
        user_state_status = "unchanged"

    # Fix ownership
    chown_recursive(config.claude_dir, config.runtime_uid, config.runtime_gid)
    chown_recursive(
        config.user_home / ".config/claude-code", config.runtime_uid, config.runtime_gid
    )
    config.claude_dir.chmod(0o700)

    logger.success(f"Claude settings created: {settings_file}")
    logger.success(f"Claude user state {user_state_status}: {user_state_file}")
    if not config.quiet:
        print(json.dumps(settings, indent=2))
        print()


def setup_bashrc(config: Config, logger: Logger) -> None:
    """Set up .bashrc with aliases."""
    bashrc = config.user_home / ".bashrc"

    # Append our settings
    with open(bashrc, "a") as f:
        f.write("\n# Added by egg entrypoint\n")
        f.write("alias claude='claude --dangerously-skip-permissions'\n")
        f.write(
            r"export PS1='\[\033[01;32m\]\u@sandboxed\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '"
            + "\n"
        )

    os.chown(bashrc, config.runtime_uid, config.runtime_gid)
    logger.success("Claude alias created (bypasses permissions in sandbox)")
