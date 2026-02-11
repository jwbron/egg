"""
egg-orchestrator: SDLC Pipeline Orchestration Service.

This package provides the orchestration layer for managing SDLC pipelines locally,
serving as a drop-in replacement for GitHub Actions orchestration.

Key components:
- Pipeline state management (git-backed persistence)
- Container lifecycle management (spawn/monitor/cleanup sandboxes)
- HITL decision queue (human-in-the-loop integration)
- REST API for sandbox communication
"""

__version__ = "0.1.0"
