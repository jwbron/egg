"""Shared test configuration.

Ensures that the repo's shared/ directory takes precedence over the
runtime installation at /opt/egg-runtime/shared/ so that tests run
against the local (potentially modified) code.
"""

import sys
from pathlib import Path

# Insert repo's shared/ at the front of sys.path so local modifications
# (e.g., new modules like egg_agent.tool_interceptor) are importable
# even when an older version is installed at /opt/egg-runtime/shared/.
_shared_dir = str(Path(__file__).parent.parent)
if _shared_dir not in sys.path[:3]:
    sys.path.insert(0, _shared_dir)
