"""Self-improvement utilities for egg.

This module provides log collection utilities for egg's self-improvement cycle.
The actual analysis and issue creation is handled by egg itself, following
agent-mode design principles (see docs/guides/agent-mode-design.md).

These collectors are provided as utilities that egg or developers can use
when direct gh CLI access isn't available or convenient.

The collect module provides pre-collection of run data for use in workflows,
providing orientation context while still allowing the agent to explore further.
"""

from .collect import collect_run_summary as collect_run_summary
from .collect import format_markdown_summary as format_markdown_summary
from .collectors.base import LogCollector as LogCollector
from .collectors.base import RunLog as RunLog
