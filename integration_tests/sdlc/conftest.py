"""Pytest configuration for SDLC integration tests.

Handles path setup for the shared egg_contracts module.
"""

import sys
from pathlib import Path

# Add shared directory to path for egg_contracts imports
# This is done once here rather than in each test file
_project_root = Path(__file__).parent.parent.parent
_shared_path = _project_root / "shared"
if str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))
