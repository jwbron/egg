"""Pytest fixtures for egg_contracts tests."""

import sys
import tempfile
from pathlib import Path

import pytest

# Add shared to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "shared"))


@pytest.fixture
def temp_repo():
    """Create a temporary directory simulating a repo root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)
        # Create .egg/contracts directory
        contracts_dir = repo_root / ".egg" / "contracts"
        contracts_dir.mkdir(parents=True)
        yield repo_root


@pytest.fixture
def sample_contract():
    """Create a sample contract for testing."""
    from egg_contracts.models import (
        Contract,
        Decision,
        DecisionType,
        Issue,
        Phase,
        Task,
    )

    return Contract(
        issue=Issue(
            number=123,
            title="Test Issue",
            url="https://github.com/owner/repo/issues/123",
        ),
        branch="egg/issue-123",
        phases=[
            Phase(
                id="phase-1",
                name="Implementation",
                tasks=[
                    Task(id="task-1", description="Create schema"),
                    Task(id="task-2", description="Implement models"),
                ],
            ),
        ],
        decisions=[
            Decision(id="decision-1", question="Approve plan?", type=DecisionType.HITL),
        ],
    )
