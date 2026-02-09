#!/usr/bin/env python3
"""
Migrate existing contracts to the new schema with phase_config support.

This script performs additive migrations only - it adds default phase_config
to contracts that don't have it, without breaking existing fields.

Usage:
    python scripts/migrate-contracts.py [--dry-run] [--contract-path PATH]

Options:
    --dry-run           Show what would be changed without modifying files
    --contract-path     Path to a specific contract file (default: all contracts in .egg-state/contracts/)
"""

import argparse
import json
import sys
from pathlib import Path

# Default phase configurations for each work loop phase
DEFAULT_PHASE_CONFIG = {
    "refine": {
        "producer_prompt_script": "action/build-sdlc-prompt.sh",
        "producer_timeout_minutes": 60,
        "reviewer_prompt_script": "action/build-refine-review-prompt.sh",
        "reviewer_timeout_minutes": 30,
        "max_cycles": 3,
        "intermediate_checks": [],
        "human_review_mechanism": "issue_comment",
        "output_artifact_path": ".egg-state/drafts/{issue}-analysis.md",
        "post_producer_script": None,
    },
    "plan": {
        "producer_prompt_script": "action/build-sdlc-prompt.sh",
        "producer_timeout_minutes": 60,
        "reviewer_prompt_script": "action/build-plan-review-prompt.sh",
        "reviewer_timeout_minutes": 30,
        "max_cycles": 3,
        "intermediate_checks": [],
        "human_review_mechanism": "issue_comment",
        "output_artifact_path": ".egg-state/drafts/{issue}-plan.md",
        "post_producer_script": "action/populate-contract-tasks.py",
    },
    "implement": {
        "producer_prompt_script": "action/build-sdlc-prompt.sh",
        "producer_timeout_minutes": 360,
        "reviewer_prompt_script": None,  # Uses PR-based review
        "reviewer_timeout_minutes": 30,
        "max_cycles": 3,
        "intermediate_checks": [
            {
                "id": "check-lint",
                "name": "Run Linter",
                "command": "make lint",
                "auto_fix": True,
                "auto_fix_command": "make fix",
                "depends_on": [],
                "required": True,
                "timeout_minutes": 10,
            },
            {
                "id": "check-test",
                "name": "Run Tests",
                "command": "make test",
                "auto_fix": False,
                "auto_fix_command": None,
                "depends_on": ["check-lint"],
                "required": True,
                "timeout_minutes": 30,
            },
        ],
        "human_review_mechanism": "pr_review",
        "output_artifact_path": None,
        "post_producer_script": None,
    },
}


def migrate_contract(contract: dict, dry_run: bool = False) -> tuple[dict, list[str]]:
    """
    Migrate a contract to include phase_config if missing.

    Args:
        contract: The contract dictionary to migrate
        dry_run: If True, don't modify the contract

    Returns:
        Tuple of (migrated_contract, list of changes made)
    """
    changes = []

    # Check if phase_config already exists
    if "phase_config" not in contract or contract["phase_config"] is None:
        changes.append("Added phase_config with default configurations for refine, plan, implement")
        if not dry_run:
            contract["phase_config"] = DEFAULT_PHASE_CONFIG.copy()

    # Check if work_loop_state field should be added (as null default)
    if "work_loop_state" not in contract:
        changes.append("Added work_loop_state field (null)")
        if not dry_run:
            contract["work_loop_state"] = None

    return contract, changes


def migrate_contract_file(file_path: Path, dry_run: bool = False) -> bool:
    """
    Migrate a contract file.

    Args:
        file_path: Path to the contract JSON file
        dry_run: If True, don't modify the file

    Returns:
        True if changes were made (or would be made in dry-run), False otherwise
    """
    print(f"\nProcessing: {file_path}")

    try:
        with open(file_path) as f:
            contract = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  ERROR: Invalid JSON: {e}")
        return False
    except OSError as e:
        print(f"  ERROR: Could not read file: {e}")
        return False

    # Get issue number for logging
    issue_number = contract.get("issue", {}).get("number", "unknown")
    print(f"  Issue: #{issue_number}")

    # Migrate the contract
    migrated, changes = migrate_contract(contract, dry_run)

    if not changes:
        print("  No changes needed - already migrated")
        return False

    print(f"  Changes ({'would be ' if dry_run else ''}made):")
    for change in changes:
        print(f"    - {change}")

    if not dry_run:
        # Write back the migrated contract
        try:
            with open(file_path, "w") as f:
                json.dump(migrated, f, indent=2)
                f.write("\n")  # Ensure trailing newline
            print("  File updated successfully")
        except OSError as e:
            print(f"  ERROR: Could not write file: {e}")
            return False

    return True


def find_contract_files(base_path: Path) -> list[Path]:
    """Find all contract JSON files in the given directory."""
    contracts_dir = base_path / ".egg-state" / "contracts"
    if not contracts_dir.exists():
        return []

    return list(contracts_dir.glob("*.json"))


def main():
    parser = argparse.ArgumentParser(
        description="Migrate existing contracts to the new schema with phase_config support"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying files",
    )
    parser.add_argument(
        "--contract-path",
        type=Path,
        help="Path to a specific contract file (default: all contracts)",
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=Path.cwd(),
        help="Path to the repository root (default: current directory)",
    )

    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN MODE ===")
        print("No files will be modified\n")

    # Determine which files to migrate
    if args.contract_path:
        if not args.contract_path.exists():
            print(f"ERROR: Contract file not found: {args.contract_path}")
            sys.exit(1)
        contract_files = [args.contract_path]
    else:
        contract_files = find_contract_files(args.repo_path)
        if not contract_files:
            print(f"No contract files found in {args.repo_path / '.egg-state' / 'contracts'}")
            sys.exit(0)

    print(f"Found {len(contract_files)} contract file(s) to check")

    # Migrate each contract
    migrated_count = 0
    for contract_file in contract_files:
        if migrate_contract_file(contract_file, args.dry_run):
            migrated_count += 1

    # Summary
    print(f"\n{'=' * 50}")
    if args.dry_run:
        print(f"Would migrate {migrated_count} of {len(contract_files)} contract(s)")
    else:
        print(f"Migrated {migrated_count} of {len(contract_files)} contract(s)")

    sys.exit(0)


if __name__ == "__main__":
    main()
