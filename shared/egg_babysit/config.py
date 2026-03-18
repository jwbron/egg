"""Configuration for the babysit-pr loop."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BabysitConfig:
    """Configuration for babysit-pr loop.

    Attributes:
        pr_number: GitHub PR number to babysit.
        repo: Repository in owner/repo format.
        timeout_seconds: Maximum wall-clock time before timeout exit.
        max_iterations: Maximum number of fix-check-review iterations.
        poll_interval_seconds: Seconds between CI status polls.
        max_retries_per_job: Default max retries per failing CI job.
        max_feedback_rounds: Maximum rounds of review feedback addressing.
        check_fixers_path: Path to check-fixers.yml config. Auto-detected if empty.
        orchestrator_url: Orchestrator API URL. Auto-detected from env if empty.
        pipeline_id: Pipeline ID for orchestrator. Auto-generated as pr-{N} if empty.
    """

    pr_number: int
    repo: str
    timeout_seconds: int = 14400  # 4 hours default
    max_iterations: int = 10
    poll_interval_seconds: int = 30
    max_retries_per_job: int = 3
    max_feedback_rounds: int = 5
    check_fixers_path: str = ""
    orchestrator_url: str = ""
    pipeline_id: str = ""

    def __post_init__(self) -> None:
        """Validate configuration bounds."""
        if self.timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {self.timeout_seconds}")
        if self.max_iterations <= 0:
            raise ValueError(f"max_iterations must be positive, got {self.max_iterations}")
        if self.poll_interval_seconds <= 0:
            raise ValueError(
                f"poll_interval_seconds must be positive, got {self.poll_interval_seconds}"
            )
        if self.max_retries_per_job < 0:
            raise ValueError(
                f"max_retries_per_job must be non-negative, got {self.max_retries_per_job}"
            )
        if self.max_feedback_rounds < 0:
            raise ValueError(
                f"max_feedback_rounds must be non-negative, got {self.max_feedback_rounds}"
            )


__all__ = [
    "BabysitConfig",
]
