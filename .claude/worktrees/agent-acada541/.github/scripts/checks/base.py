"""
Base class for check runners.

All check scripts should inherit from CheckRunner and implement the run() method.
"""

import json
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

# Add shared directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts import CheckResult, CheckStatus, Contract


class CheckRunner(ABC):
    """Base class for check scripts.

    Subclasses must implement the run() method which executes the check
    and returns a CheckResult.
    """

    def __init__(self, contract: Contract, repo_root: Path) -> None:
        """Initialize the check runner.

        Args:
            contract: The SDLC contract for the current issue.
            repo_root: Path to the repository root.
        """
        self.contract = contract
        self.repo_root = repo_root

    @property
    @abstractmethod
    def check_id(self) -> str:
        """Return the unique ID for this check (e.g., 'check-lint')."""
        pass

    @abstractmethod
    def run(self) -> CheckResult:
        """Execute the check and return the result.

        Returns:
            CheckResult with the check outcome.
        """
        pass

    def create_result(
        self,
        status: CheckStatus,
        message: str = "",
        details: dict[str, Any] | None = None,
        fixable: bool = False,
    ) -> CheckResult:
        """Helper to create a CheckResult for this check.

        Args:
            status: The result status (pass, fail, skip).
            message: Human-readable result message.
            details: Additional details dictionary.
            fixable: Whether a failure can be auto-fixed.

        Returns:
            A CheckResult instance.
        """
        return CheckResult(
            check_id=self.check_id,
            status=status,
            message=message,
            details=details or {},
            fixable=fixable,
        )

    def output_result(self, result: CheckResult) -> None:
        """Output the result as JSON to stdout.

        Args:
            result: The CheckResult to output.
        """
        print(json.dumps(result.model_dump(mode="json")))
