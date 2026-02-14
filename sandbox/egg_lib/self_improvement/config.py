"""Configuration for self-improvement module."""

import os
from pathlib import Path

# Bot identity
BOT_USERNAME = os.getenv("EGG_BOT_USERNAME", "egg")
BOT_EMAIL = os.getenv("EGG_BOT_EMAIL", "egg@localhost")

# Metrics storage (for local development)
METRICS_DIR = Path("metrics")
METRICS_FILE = METRICS_DIR / "self-improvement-metrics.json"

# Workflow names that are egg-related
EGG_WORKFLOWS = [
    "on-pull-request.yml",
    "on-check-failure.yml",
    "on-merge-conflict.yml",
    "on-review-feedback.yml",
    "on-push-doc-updater.yml",
]

# Default time window for analysis
DEFAULT_SINCE_HOURS = 24
DEFAULT_LOG_RETENTION_DAYS = 90
