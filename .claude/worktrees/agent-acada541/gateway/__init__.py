"""Gateway Sidecar - Policy enforcement for git/gh operations.

Note: This directory has a hyphen in the name (gateway), so it cannot
be imported as a Python package. This __init__.py exists for IDE support and
documentation purposes only. The actual modules are loaded directly by conftest.py
during testing and by the entrypoint script at runtime.
"""

# Only attempt imports when this is being used as a package (which won't happen
# in practice due to the hyphen in the directory name). This prevents pytest
# from failing when it tries to collect tests.
try:
    from .gateway import app
    from .github_client import GitHubClient, get_github_client
    from .policy import PolicyEngine, get_policy_engine

    __all__ = [
        "GitHubClient",
        "PolicyEngine",
        "app",
        "get_github_client",
        "get_policy_engine",
    ]
except ImportError:
    # Running standalone (e.g., pytest collection) - imports not available
    pass
