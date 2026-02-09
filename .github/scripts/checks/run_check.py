#!/usr/bin/env python3
"""
Entry point for running check scripts.

Usage:
    python run_check.py <check_name> <contract_path>

The script loads the appropriate check class, runs it, and outputs the result as JSON.
"""

import argparse
import importlib
import json
import sys
from pathlib import Path

# Add shared directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))

from egg_contracts import CheckResult, CheckStatus, load_contract

# Mapping of check names to their module and class names
CHECK_REGISTRY: dict[str, tuple[str, str]] = {
    "merge-conflict": ("merge_conflict_check", "MergeConflictCheck"),
    "draft-validation": ("draft_validation_check", "DraftValidationCheck"),
    "plan-yaml": ("plan_yaml_check", "PlanYamlCheck"),
    "lint": ("lint_check", "LintCheck"),
    "test": ("test_check", "TestCheck"),
    "fixer": ("check_fixer", "CheckFixer"),
}


def load_check_class(check_name: str):
    """Load the check class for the given check name.

    Args:
        check_name: Name of the check (e.g., 'lint', 'merge-conflict').

    Returns:
        The check class.

    Raises:
        ValueError: If the check name is not recognized.
        ImportError: If the check module cannot be loaded.
    """
    if check_name not in CHECK_REGISTRY:
        raise ValueError(f"Unknown check: {check_name}. Available: {list(CHECK_REGISTRY.keys())}")

    module_name, class_name = CHECK_REGISTRY[check_name]
    module = importlib.import_module(f".{module_name}", package="checks")
    return getattr(module, class_name)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for running checks.

    Args:
        argv: Command line arguments (uses sys.argv if None).

    Returns:
        Exit code (0 for success, 1 for error).
    """
    parser = argparse.ArgumentParser(description="Run a check script and output results as JSON.")
    parser.add_argument(
        "check_name",
        help="Name of the check to run (e.g., 'lint', 'merge-conflict')",
    )
    parser.add_argument(
        "contract_path",
        help="Path to the contract JSON file",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repository root (default: current directory)",
    )

    args = parser.parse_args(argv)

    try:
        # Load the contract
        contract_path = Path(args.contract_path)
        if not contract_path.exists():
            error_result = CheckResult(
                check_id=f"check-{args.check_name}",
                status=CheckStatus.FAIL,
                message=f"Contract file not found: {contract_path}",
                details={},
                fixable=False,
            )
            print(json.dumps(error_result.model_dump(mode="json")))
            return 1

        # Parse issue number from contract path
        # Contract paths are typically: .egg-state/contracts/<issue_number>.json
        issue_number = int(contract_path.stem)
        contract = load_contract(issue_number, repo_root=Path(args.repo_root))

        # Load and run the check
        repo_root = Path(args.repo_root).resolve()
        check_class = load_check_class(args.check_name)
        check = check_class(contract, repo_root)
        result = check.run()

        # Output result as JSON
        print(json.dumps(result.model_dump(mode="json")))
        return 0 if result.status == CheckStatus.PASS else 1

    except ValueError as e:
        error_result = CheckResult(
            check_id=f"check-{args.check_name}",
            status=CheckStatus.FAIL,
            message=str(e),
            details={},
            fixable=False,
        )
        print(json.dumps(error_result.model_dump(mode="json")))
        return 1
    except Exception as e:
        error_result = CheckResult(
            check_id=f"check-{args.check_name}",
            status=CheckStatus.FAIL,
            message=f"Check failed with error: {e}",
            details={"error_type": type(e).__name__},
            fixable=False,
        )
        print(json.dumps(error_result.model_dump(mode="json")))
        return 1


if __name__ == "__main__":
    sys.exit(main())
