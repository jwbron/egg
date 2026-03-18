"""Conftest for babysit-pr integration tests.

Ensures shared packages are importable.
"""

import sys
from pathlib import Path

# Add shared/ to sys.path so egg_babysit can be imported.
_shared_dir = str(Path(__file__).resolve().parent.parent.parent / "shared")
if _shared_dir not in sys.path:
    sys.path.insert(0, _shared_dir)
