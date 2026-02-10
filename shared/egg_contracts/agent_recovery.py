"""
Agent recovery and error handling for multi-agent orchestration.

This module provides:
- AgentRetryManager: Retry logic for failed agents
- ConflictDetector: Detect and handle merge conflicts between parallel agents
- AgentCircuitBreaker: Circuit breaker pattern for repeated multi-agent failures

These components enable graceful handling of agent failures and coordination
issues in the multi-agent pipeline.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .agent_roles import AgentRole, AgentStatus


class RetryPolicy(StrEnum):
    """Retry policies for failed agents."""

    IMMEDIATE = "immediate"  # Retry immediately
    BACKOFF = "backoff"  # Retry with exponential backoff
    MANUAL = "manual"  # Require manual intervention
    SKIP = "skip"  # Skip this agent and continue


@dataclass
class RetryDecision:
    """Decision about whether to retry a failed agent."""

    should_retry: bool
    policy: RetryPolicy
    delay_seconds: int
    reason: str
    retry_count: int
    max_retries: int

    @classmethod
    def retry_now(cls, retry_count: int, max_retries: int) -> RetryDecision:
        """Create decision to retry immediately."""
        return cls(
            should_retry=True,
            policy=RetryPolicy.IMMEDIATE,
            delay_seconds=0,
            reason="Retrying immediately",
            retry_count=retry_count,
            max_retries=max_retries,
        )

    @classmethod
    def retry_with_backoff(
        cls, delay: int, retry_count: int, max_retries: int
    ) -> RetryDecision:
        """Create decision to retry after delay."""
        return cls(
            should_retry=True,
            policy=RetryPolicy.BACKOFF,
            delay_seconds=delay,
            reason=f"Retrying after {delay}s delay",
            retry_count=retry_count,
            max_retries=max_retries,
        )

    @classmethod
    def no_retry(cls, reason: str, retry_count: int, max_retries: int) -> RetryDecision:
        """Create decision to not retry."""
        return cls(
            should_retry=False,
            policy=RetryPolicy.MANUAL,
            delay_seconds=0,
            reason=reason,
            retry_count=retry_count,
            max_retries=max_retries,
        )


@dataclass
class AgentRetryConfig:
    """Configuration for agent retry behavior."""

    max_retries: int = 2
    initial_delay_seconds: int = 30
    max_delay_seconds: int = 300
    backoff_multiplier: float = 2.0
    retryable_errors: list[str] = field(default_factory=lambda: [
        "timeout",
        "rate_limit",
        "transient",
        "network",
    ])


class AgentRetryManager:
    """Manages retry logic for failed agents.

    Tracks retry attempts and determines whether a failed agent
    should be retried based on the error type and retry count.
    """

    def __init__(self, config: AgentRetryConfig | None = None):
        """Initialize retry manager.

        Args:
            config: Retry configuration
        """
        self.config = config or AgentRetryConfig()
        self._attempts: dict[AgentRole, int] = {}
        self._last_failures: dict[AgentRole, datetime] = {}
        self._errors: dict[AgentRole, list[str]] = {}

    def record_failure(self, role: AgentRole, error: str) -> None:
        """Record a failure for an agent.

        Args:
            role: The agent that failed
            error: Error message
        """
        self._attempts[role] = self._attempts.get(role, 0) + 1
        self._last_failures[role] = datetime.now(UTC)
        if role not in self._errors:
            self._errors[role] = []
        self._errors[role].append(error)

    def record_success(self, role: AgentRole) -> None:
        """Record a success, resetting retry counter.

        Args:
            role: The agent that succeeded
        """
        self._attempts[role] = 0
        self._errors.pop(role, None)

    def get_retry_count(self, role: AgentRole) -> int:
        """Get current retry count for an agent.

        Args:
            role: The agent role

        Returns:
            Number of retries attempted
        """
        return self._attempts.get(role, 0)

    def can_retry(self, role: AgentRole) -> bool:
        """Check if an agent can be retried.

        Args:
            role: The agent role

        Returns:
            True if retry is allowed
        """
        return self.get_retry_count(role) < self.config.max_retries

    def should_retry(self, role: AgentRole, error: str) -> RetryDecision:
        """Determine if a failed agent should be retried.

        Args:
            role: The agent that failed
            error: Error message

        Returns:
            RetryDecision with retry details
        """
        retry_count = self.get_retry_count(role)

        # Check if we've exceeded max retries
        if retry_count >= self.config.max_retries:
            return RetryDecision.no_retry(
                f"Exceeded max retries ({self.config.max_retries})",
                retry_count,
                self.config.max_retries,
            )

        # Check if error is retryable
        error_lower = error.lower()
        is_retryable = any(
            err_type in error_lower
            for err_type in self.config.retryable_errors
        )

        if not is_retryable:
            return RetryDecision.no_retry(
                f"Error not retryable: {error[:100]}",
                retry_count,
                self.config.max_retries,
            )

        # Calculate backoff delay
        delay = min(
            int(self.config.initial_delay_seconds * (self.config.backoff_multiplier ** retry_count)),
            self.config.max_delay_seconds,
        )

        return RetryDecision.retry_with_backoff(
            delay, retry_count + 1, self.config.max_retries
        )

    def reset(self, role: AgentRole | None = None) -> None:
        """Reset retry tracking.

        Args:
            role: Specific role to reset, or None to reset all
        """
        if role:
            self._attempts.pop(role, None)
            self._last_failures.pop(role, None)
            self._errors.pop(role, None)
        else:
            self._attempts.clear()
            self._last_failures.clear()
            self._errors.clear()

    def get_status(self) -> dict[str, Any]:
        """Get current retry status for all agents.

        Returns:
            Dictionary with retry status
        """
        return {
            role.value: {
                "attempts": count,
                "last_failure": self._last_failures.get(role, None),
                "errors": self._errors.get(role, []),
                "can_retry": count < self.config.max_retries,
            }
            for role, count in self._attempts.items()
        }


@dataclass
class ConflictInfo:
    """Information about a detected conflict."""

    conflicting_files: list[str]
    agents_involved: list[AgentRole]
    conflict_type: str  # "merge", "edit", "delete"
    resolution_hint: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "conflicting_files": self.conflicting_files,
            "agents_involved": [a.value for a in self.agents_involved],
            "conflict_type": self.conflict_type,
            "resolution_hint": self.resolution_hint,
            "detected_at": self.detected_at.isoformat(),
        }


class ConflictDetector:
    """Detects and helps resolve conflicts between parallel agents.

    When agents run in parallel, they may modify overlapping files or
    create merge conflicts. This detector identifies these issues.
    """

    def __init__(self, repo_path: Path):
        """Initialize conflict detector.

        Args:
            repo_path: Path to the git repository
        """
        self.repo_path = repo_path
        self._detected_conflicts: list[ConflictInfo] = []

    def check_for_merge_conflicts(self) -> list[ConflictInfo]:
        """Check for git merge conflicts in the repository.

        Returns:
            List of detected conflicts
        """
        conflicts = []

        try:
            # Check for merge conflict markers
            result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0 and result.stdout.strip():
                conflicting_files = result.stdout.strip().split("\n")
                conflict = ConflictInfo(
                    conflicting_files=conflicting_files,
                    agents_involved=[],  # Unknown at this point
                    conflict_type="merge",
                    resolution_hint="Manual merge resolution required",
                )
                conflicts.append(conflict)
                self._detected_conflicts.append(conflict)

        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            pass

        return conflicts

    def check_file_overlap(
        self,
        agent1_files: list[str],
        agent1_role: AgentRole,
        agent2_files: list[str],
        agent2_role: AgentRole,
    ) -> list[ConflictInfo]:
        """Check for overlapping file modifications between agents.

        Args:
            agent1_files: Files modified by first agent
            agent1_role: Role of first agent
            agent2_files: Files modified by second agent
            agent2_role: Role of second agent

        Returns:
            List of detected conflicts
        """
        conflicts = []

        overlap = set(agent1_files) & set(agent2_files)
        if overlap:
            conflict = ConflictInfo(
                conflicting_files=list(overlap),
                agents_involved=[agent1_role, agent2_role],
                conflict_type="edit",
                resolution_hint=(
                    f"Both {agent1_role.value} and {agent2_role.value} "
                    f"modified the same files. Review changes carefully."
                ),
            )
            conflicts.append(conflict)
            self._detected_conflicts.append(conflict)

        return conflicts

    def detect_conflicts_from_outputs(
        self,
        agent_outputs: dict[AgentRole, dict[str, Any]],
    ) -> list[ConflictInfo]:
        """Detect conflicts by analyzing agent outputs.

        Args:
            agent_outputs: Mapping of agent roles to their output data

        Returns:
            List of detected conflicts
        """
        conflicts = []

        # Extract changed files from each agent
        agent_files: dict[AgentRole, list[str]] = {}
        for role, output in agent_outputs.items():
            files = output.get("changed_files", [])
            if files:
                agent_files[role] = files

        # Check for overlaps between all pairs
        roles = list(agent_files.keys())
        for i, role1 in enumerate(roles):
            for role2 in roles[i + 1:]:
                pair_conflicts = self.check_file_overlap(
                    agent_files[role1], role1,
                    agent_files[role2], role2,
                )
                conflicts.extend(pair_conflicts)

        return conflicts

    def get_all_conflicts(self) -> list[ConflictInfo]:
        """Get all detected conflicts.

        Returns:
            List of all conflicts detected
        """
        return self._detected_conflicts.copy()

    def clear_conflicts(self) -> None:
        """Clear all recorded conflicts."""
        self._detected_conflicts.clear()

    def has_conflicts(self) -> bool:
        """Check if any conflicts have been detected.

        Returns:
            True if conflicts exist
        """
        return len(self._detected_conflicts) > 0


class CircuitState(StrEnum):
    """States for the circuit breaker."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Blocking operations due to failures
    HALF_OPEN = "half_open"  # Testing if operations can resume


@dataclass
class CircuitBreakerConfig:
    """Configuration for the circuit breaker."""

    failure_threshold: int = 3  # Failures before opening
    reset_timeout_seconds: int = 300  # Time before trying half-open
    success_threshold: int = 2  # Successes needed to close from half-open


class AgentCircuitBreaker:
    """Circuit breaker for multi-agent failures.

    Triggers when multiple agents fail repeatedly, preventing
    wasted compute and allowing time for issues to be resolved.
    """

    def __init__(self, config: CircuitBreakerConfig | None = None):
        """Initialize circuit breaker.

        Args:
            config: Circuit breaker configuration
        """
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: datetime | None = None
        self._opened_at: datetime | None = None
        self._failed_agents: list[AgentRole] = []

    @property
    def state(self) -> CircuitState:
        """Get current circuit state (read-only)."""
        return self._state

    def _maybe_transition_to_half_open(self) -> None:
        """Transition from OPEN to HALF_OPEN if the reset timeout has elapsed.

        This is called before checking whether operations can execute,
        keeping the state property side-effect-free.
        """
        if self._state == CircuitState.OPEN and self._opened_at:
            elapsed = (datetime.now(UTC) - self._opened_at).total_seconds()
            if elapsed >= self.config.reset_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0

    def is_open(self) -> bool:
        """Check if circuit is open (blocking operations).

        Returns:
            True if operations should be blocked
        """
        self._maybe_transition_to_half_open()
        return self._state == CircuitState.OPEN

    def can_execute(self) -> bool:
        """Check if an operation can be executed.

        Returns:
            True if operation is allowed
        """
        self._maybe_transition_to_half_open()
        return self._state != CircuitState.OPEN

    def record_failure(self, role: AgentRole, error: str | None = None) -> None:
        """Record an agent failure.

        Args:
            role: The agent that failed
            error: Optional error message
        """
        self._maybe_transition_to_half_open()
        self._failure_count += 1
        self._last_failure_time = datetime.now(UTC)
        if role not in self._failed_agents:
            self._failed_agents.append(role)

        # Check if we should open the circuit
        if self._state == CircuitState.CLOSED:
            if self._failure_count >= self.config.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = datetime.now(UTC)
        elif self._state == CircuitState.HALF_OPEN:
            # Failed during test - back to open
            self._state = CircuitState.OPEN
            self._opened_at = datetime.now(UTC)

    def record_success(self, role: AgentRole) -> None:
        """Record an agent success.

        Args:
            role: The agent that succeeded
        """
        self._maybe_transition_to_half_open()
        if role in self._failed_agents:
            self._failed_agents.remove(role)

        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.config.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._success_count = 0
                self._failed_agents.clear()

    def reset(self) -> None:
        """Reset the circuit breaker to closed state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        self._opened_at = None
        self._failed_agents.clear()

    def get_status(self) -> dict[str, Any]:
        """Get current circuit breaker status.

        Returns:
            Dictionary with status information
        """
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.config.failure_threshold,
            "success_count": self._success_count,
            "last_failure": (
                self._last_failure_time.isoformat()
                if self._last_failure_time else None
            ),
            "opened_at": (
                self._opened_at.isoformat()
                if self._opened_at else None
            ),
            "failed_agents": [a.value for a in self._failed_agents],
            "can_execute": self.can_execute(),
        }

    def get_failure_summary(self) -> str:
        """Get a human-readable failure summary.

        Returns:
            Summary string
        """
        if not self._failed_agents:
            return "No agent failures recorded"

        agents = ", ".join(a.value for a in self._failed_agents)
        return (
            f"Circuit is {self.state.value}. "
            f"{self._failure_count} failures from agents: {agents}"
        )


def create_retry_manager(
    max_retries: int = 2,
    initial_delay: int = 30,
) -> AgentRetryManager:
    """Create a retry manager with custom settings.

    Args:
        max_retries: Maximum retry attempts
        initial_delay: Initial delay in seconds

    Returns:
        Configured AgentRetryManager
    """
    config = AgentRetryConfig(
        max_retries=max_retries,
        initial_delay_seconds=initial_delay,
    )
    return AgentRetryManager(config)


def create_circuit_breaker(
    failure_threshold: int = 3,
    reset_timeout: int = 300,
) -> AgentCircuitBreaker:
    """Create a circuit breaker with custom settings.

    Args:
        failure_threshold: Failures before opening
        reset_timeout: Seconds before half-open

    Returns:
        Configured AgentCircuitBreaker
    """
    config = CircuitBreakerConfig(
        failure_threshold=failure_threshold,
        reset_timeout_seconds=reset_timeout,
    )
    return AgentCircuitBreaker(config)
