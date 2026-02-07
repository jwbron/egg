"""Configuration for self-improvement analysis.

This module contains configurable constants and defaults for the
self-improvement analysis system.
"""

import os
from pathlib import Path

# Bot identity - configurable via environment
BOT_USERNAME = os.getenv("EGG_BOT_USERNAME", "james-in-a-box[bot]")
BOT_EMAIL = os.getenv("EGG_BOT_EMAIL", "egg@localhost")

# Metrics storage
METRICS_DIR = Path("metrics")
METRICS_FILE = METRICS_DIR / "self-improvement-metrics.json"

# Issue creation
ISSUE_LABEL_PREFIX = "self-improvement"
ISSUE_TITLE_PREFIX = "[Self-Improvement]"

# Analysis defaults
DEFAULT_SINCE_HOURS = 24
DEFAULT_LOG_RETENTION_DAYS = 90

# Workflows to analyze (egg-triggered workflows)
# Can be overridden via EGG_WORKFLOWS env var (comma-separated list)
_default_workflows = [
    "on-mention.yml",
    "on-pull-request.yml",
]
_workflows_env = os.getenv("EGG_WORKFLOWS")
EGG_WORKFLOWS = (
    [w.strip() for w in _workflows_env.split(",") if w.strip()]
    if _workflows_env
    else _default_workflows
)
