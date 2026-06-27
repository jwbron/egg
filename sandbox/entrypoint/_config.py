"""Container Config dataclass and Logger."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ._core import _CONTAINER_START_TIME


@dataclass
class Config:
    """Container configuration from environment variables."""

    # Fixed container user - UID/GID adjusted at runtime to match host
    container_user: str = "egg"
    runtime_uid: int = field(default_factory=lambda: int(os.environ.get("RUNTIME_UID", "1000")))
    runtime_gid: int = field(default_factory=lambda: int(os.environ.get("RUNTIME_GID", "1000")))
    quiet: bool = field(default_factory=lambda: os.environ.get("EGG_QUIET", "0") == "1")
    debug: bool = field(default_factory=lambda: os.environ.get("EGG_DEBUG", "0") == "1")

    # Orchestrator mode configuration
    # These are set when the sandbox is spawned by an orchestrator
    orchestrator_mode: str = field(
        default_factory=lambda: os.environ.get("EGG_ORCHESTRATOR_MODE", "local")
    )
    orchestrator_url: str | None = field(
        default_factory=lambda: os.environ.get("EGG_ORCHESTRATOR_URL")
    )
    pipeline_id: str | None = field(default_factory=lambda: os.environ.get("EGG_PIPELINE_ID"))
    agent_role: str | None = field(default_factory=lambda: os.environ.get("EGG_AGENT_ROLE"))

    @property
    def is_orchestrator_mode(self) -> bool:
        """Check if running in orchestrator-managed mode (vs interactive)."""
        # Explicit mode setting
        if self.orchestrator_mode in ("remote-single", "distributed"):
            return True
        # Implicit detection from pipeline context
        if self.pipeline_id:
            return True
        # Implicit detection from orchestrator URL
        if self.orchestrator_url:
            return True
        return False

    # LLM configuration
    # Auth method: "api_key" (default) or "oauth"
    # When oauth, don't warn about missing ANTHROPIC_API_KEY
    anthropic_auth_method: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_AUTH_METHOD", "api_key").lower()
    )

    # Valid auth methods for validation
    VALID_AUTH_METHODS: ClassVar[tuple[str, ...]] = ("api_key", "oauth")
    anthropic_api_key: str | None = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY")
    )

    # GitHub tokens
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN"))
    github_readonly_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_READONLY_TOKEN")
    )

    # Derived paths - fixed home directory for egg user
    @property
    def user_home(self) -> Path:
        return Path("/home/egg")

    @property
    def repos_dir(self) -> Path:
        """The directory containing mounted repositories."""
        return self.user_home / "repos"

    @property
    def claude_dir(self) -> Path:
        return self.user_home / ".claude"


class Logger:
    """Simple logger with quiet mode and debug mode support.

    Debug mode (EGG_DEBUG=1) logs each startup phase to stderr for
    post-mortem analysis when containers hang or timeout.
    """

    def __init__(self, quiet: bool = False, debug: bool = False):
        self.quiet = quiet
        self.debug = debug

    def info(self, msg: str) -> None:
        """Info message (hidden in quiet mode)."""
        if not self.quiet:
            print(msg)

    def success(self, msg: str) -> None:
        """Success message with checkmark (hidden in quiet mode)."""
        if not self.quiet:
            print(f"✓ {msg}")

    def warn(self, msg: str) -> None:
        """Warning message (always shown)."""
        print(f"⚠ {msg}")

    def error(self, msg: str) -> None:
        """Error message (always shown, to stderr)."""
        print(f"✗ {msg}", file=sys.stderr)

    def phase_start(self, phase: str) -> None:
        """Log the start of a startup phase (debug mode only, to stderr).

        These messages go to stderr so they're captured even if the
        container hangs during startup (stdout may be buffered).
        """
        if self.debug:
            elapsed_ms = (time.time() - _CONTAINER_START_TIME) * 1000
            print(f"[DEBUG +{elapsed_ms:7.0f}ms] Starting: {phase}", file=sys.stderr)
            sys.stderr.flush()

    def phase_end(self, phase: str) -> None:
        """Log the end of a startup phase (debug mode only, to stderr)."""
        if self.debug:
            elapsed_ms = (time.time() - _CONTAINER_START_TIME) * 1000
            print(f"[DEBUG +{elapsed_ms:7.0f}ms] Finished: {phase}", file=sys.stderr)
            sys.stderr.flush()
