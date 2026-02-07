"""Self-improvement utilities for egg.

This module provides log collection utilities for egg's self-improvement cycle.
The actual analysis and issue creation is handled by egg itself, following
agent-mode design principles (see docs/guides/agent-mode-design.md).

These collectors are provided as utilities that egg or developers can use
when direct gh CLI access isn't available or convenient.
"""

from .collectors.base import LogCollector as LogCollector
from .collectors.base import RunLog as RunLog
