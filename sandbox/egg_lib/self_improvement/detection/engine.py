"""Detection engine for analyzing logs and identifying issues.

This module provides the core detection logic that scans collected logs
for error patterns, inefficiencies, and behavioral issues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..collectors.base import RunLog


class Severity(Enum):
    """Severity levels for detected issues."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Detection:
    """A detected issue from log analysis.

    Attributes:
        rule_id: Unique identifier for the detection rule
        category: Category of issue (e.g., "environment_errors", "tool_failures")
        title: Human-readable title for the issue
        description: Detailed description of the problem
        severity: Severity level
        pattern: The pattern that matched (for debugging)
        evidence: Sample log lines that triggered the detection
        run_ids: IDs of runs where this issue was detected
        occurrence_count: Total number of times the pattern was matched
        recommendation: Suggested action to resolve the issue
    """

    rule_id: str
    category: str
    title: str
    description: str
    severity: Severity
    pattern: str
    evidence: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    occurrence_count: int = 0
    recommendation: str = ""


@dataclass
class DetectionRule:
    """A rule for detecting issues in logs.

    Attributes:
        id: Unique rule identifier
        category: Category of issue
        pattern: Regex pattern to match
        severity: Base severity level
        title: Issue title template
        description: Issue description template
        threshold: Minimum occurrences per run before triggering (default: 1)
        recommendation: Suggested fix
    """

    id: str
    category: str
    pattern: str
    severity: Severity
    title: str
    description: str
    threshold: int = 1
    recommendation: str = ""

    def __post_init__(self) -> None:
        """Compile the regex pattern after initialization."""
        self._compiled_pattern: re.Pattern[str] = re.compile(
            self.pattern, re.IGNORECASE | re.MULTILINE
        )

    @property
    def compiled_pattern(self) -> re.Pattern[str]:
        """Return the compiled regex pattern."""
        return self._compiled_pattern


class DetectionEngine:
    """Engine for running detection rules against collected logs.

    The engine loads rules from YAML files and applies them to RunLog
    instances to identify issues.
    """

    def __init__(self, rules_dir: Path | None = None) -> None:
        """Initialize the detection engine.

        Args:
            rules_dir: Directory containing YAML rule files.
                       Defaults to the 'rules' subdirectory.
        """
        if rules_dir is None:
            rules_dir = Path(__file__).parent / "rules"
        self.rules_dir = rules_dir
        self.rules: list[DetectionRule] = []
        self._load_rules()

    def _load_rules(self) -> None:
        """Load detection rules from YAML files."""
        self.rules = []

        if not self.rules_dir.exists():
            return

        for yaml_file in self.rules_dir.glob("*.yaml"):
            try:
                self._load_rules_from_file(yaml_file)
            except (yaml.YAMLError, KeyError, ValueError) as e:
                # Log but don't fail on malformed rule files
                import sys

                print(f"Warning: Failed to load rules from {yaml_file}: {e}", file=sys.stderr)

    def _load_rules_from_file(self, filepath: Path) -> None:
        """Load rules from a single YAML file.

        Args:
            filepath: Path to the YAML file
        """
        with open(filepath) as f:
            data = yaml.safe_load(f)

        if not data:
            return

        # Each top-level key is a category
        for category, rules_list in data.items():
            if not isinstance(rules_list, list):
                continue

            for rule_data in rules_list:
                rule = DetectionRule(
                    id=rule_data.get("id", f"{category}_{len(self.rules)}"),
                    category=category,
                    pattern=rule_data["pattern"],
                    severity=Severity(rule_data.get("severity", "medium").lower()),
                    title=rule_data.get("title", rule_data.get("issue_title", "Detected issue")),
                    description=rule_data.get(
                        "description", f"Pattern matched: {rule_data['pattern']}"
                    ),
                    threshold=rule_data.get("threshold", 1),
                    recommendation=rule_data.get("recommendation", ""),
                )
                self.rules.append(rule)

    def analyze(self, runs: list[RunLog]) -> list[Detection]:
        """Analyze collected runs and return detected issues.

        Args:
            runs: List of RunLog instances to analyze

        Returns:
            List of Detection instances for identified issues
        """
        # Track detections by rule_id to aggregate across runs
        detection_map: dict[str, Detection] = {}

        for run in runs:
            self._analyze_run(run, detection_map)

        # Apply severity escalation based on occurrence count
        detections = list(detection_map.values())
        for detection in detections:
            self._escalate_severity(detection)

        # Sort by severity (HIGH first) and occurrence count
        severity_order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
        detections.sort(key=lambda d: (severity_order[d.severity], -d.occurrence_count))

        return detections

    def _analyze_run(self, run, detection_map: dict[str, Detection]) -> None:
        """Analyze a single run against all rules.

        Args:
            run: A RunLog instance
            detection_map: Map of rule_id -> Detection to aggregate results
        """
        logs = run.logs

        for rule in self.rules:
            matches = list(rule.compiled_pattern.finditer(logs))
            match_count = len(matches)

            if match_count >= rule.threshold:
                if rule.id not in detection_map:
                    detection_map[rule.id] = Detection(
                        rule_id=rule.id,
                        category=rule.category,
                        title=rule.title,
                        description=rule.description,
                        severity=rule.severity,
                        pattern=rule.pattern,
                        recommendation=rule.recommendation,
                    )

                detection = detection_map[rule.id]
                detection.occurrence_count += match_count
                detection.run_ids.append(run.run_id)

                # Collect evidence (limit to avoid huge lists)
                for match in matches[:3]:
                    evidence_line = self._extract_context(logs, match.start(), max_chars=200)
                    if evidence_line and evidence_line not in detection.evidence:
                        detection.evidence.append(evidence_line)
                        if len(detection.evidence) >= 10:
                            break

    def _extract_context(self, logs: str, position: int, max_chars: int = 200) -> str:
        """Extract context around a match position.

        Args:
            logs: Full log content
            position: Match position in the logs
            max_chars: Maximum characters to extract

        Returns:
            Context string around the match
        """
        # Find the line containing the match
        start = logs.rfind("\n", 0, position) + 1
        end = logs.find("\n", position)
        if end == -1:
            end = len(logs)

        line = logs[start:end].strip()

        # Truncate if too long
        if len(line) > max_chars:
            line = line[:max_chars] + "..."

        return line

    def _escalate_severity(self, detection: Detection) -> None:
        """Escalate severity based on occurrence frequency.

        Escalation rules:
        - 6+ occurrences: LOW -> HIGH, MEDIUM -> HIGH
        - 3-5 occurrences: LOW -> MEDIUM

        Args:
            detection: Detection to potentially escalate
        """
        if detection.occurrence_count >= 6:
            if detection.severity in (Severity.LOW, Severity.MEDIUM):
                detection.severity = Severity.HIGH
        elif detection.occurrence_count >= 3:
            if detection.severity == Severity.LOW:
                detection.severity = Severity.MEDIUM

    def get_rules_by_severity(self, severity: Severity) -> list[DetectionRule]:
        """Get all rules of a specific severity level.

        Args:
            severity: Severity level to filter by

        Returns:
            List of matching rules
        """
        return [r for r in self.rules if r.severity == severity]

    def get_high_severity_rules(self) -> list[DetectionRule]:
        """Get all HIGH severity rules.

        Returns:
            List of HIGH severity rules
        """
        return self.get_rules_by_severity(Severity.HIGH)
