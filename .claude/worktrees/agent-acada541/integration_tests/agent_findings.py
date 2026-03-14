"""Agent findings logger for integration tests.

Records agent-led test findings to a JSONL log so that reproducible
edge cases can be codified as deterministic tests over time.

Workflow:
  1. Agent fuzz tests call ``record_finding()`` with verdict data.
  2. Findings are appended to ``integration_tests/findings/<date>.jsonl``
     (or ``$AGENT_FINDINGS_DIR`` if set).
  3. CI uploads the findings directory as a workflow artifact.
  4. A human reviews findings, writes deterministic tests for
     reproducible ones, and marks them ``codified=True``.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

_DEFAULT_DIR = Path(__file__).parent / "findings"


def _findings_dir() -> Path:
    """Return the directory for findings output."""
    env = os.environ.get("AGENT_FINDINGS_DIR")
    if env:
        return Path(env)
    return _DEFAULT_DIR


def record_finding(
    test_name: str,
    verdict: Any,
    *,
    category: str = "general",
    codified: bool = False,
) -> Path:
    """Append a finding to the JSONL log.

    Args:
        test_name: Fully qualified test name.
        verdict: An ``AgentVerdict`` or dict with verdict data.
        category: Classification (e.g. "security", "network", "general").
        codified: True if a deterministic test already covers this finding.

    Returns:
        Path to the findings file that was written to.
    """
    out_dir = _findings_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    date_str = time.strftime("%Y-%m-%d")
    out_file = out_dir / f"{date_str}.jsonl"

    # Normalise verdict to dict
    if hasattr(verdict, "__dataclass_fields__"):
        from dataclasses import asdict

        verdict_data = asdict(verdict)
    elif isinstance(verdict, dict):
        verdict_data = verdict
    else:
        verdict_data = {"raw": str(verdict)}

    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "test_name": test_name,
        "category": category,
        "verdict": verdict_data.get("verdict", "unknown"),
        "evidence": verdict_data.get("evidence", ""),
        "details": verdict_data.get("details", []),
        "cost_usd": verdict_data.get("cost_usd"),
        "codified": codified,
    }

    with open(out_file, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return out_file
