"""
Test configuration for gateway tests.

Uses importlib to properly load modules since the directory name (gateway)
contains a hyphen which is not valid for Python package names.

All modules with relative imports are loaded with those imports converted to
absolute imports that resolve to our loaded modules.
"""

import os
import sys
from pathlib import Path
from types import ModuleType

# Set up test secrets before any gateway imports
TEST_LAUNCHER_SECRET = "test-launcher-secret-12345"
os.environ["EGG_LAUNCHER_SECRET"] = TEST_LAUNCHER_SECRET

# Set required gateway policy configuration for tests
os.environ["GATEWAY_BOT_NAME"] = "egg"
os.environ["GATEWAY_BOT_BRANCH_PREFIX"] = "egg"

# Create a minimal test repositories.yaml config
import tempfile

_test_config_dir = tempfile.mkdtemp()
_test_config_path = Path(_test_config_dir) / "repositories.yaml"
_test_config_path.write_text(
    """
github:
  username: test-user
  writable:
    - test-user/test-repo
    - owner/repo
  default_reviewer: reviewer
"""
)
os.environ["EGG_REPO_CONFIG"] = str(_test_config_path)

# Get the gateway directory
GATEWAY_DIR = Path(__file__).parent.parent
REPO_ROOT = GATEWAY_DIR.parent
SHARED_DIR = REPO_ROOT / "shared"

# Add shared to path for egg_logging
sys.path.insert(0, str(SHARED_DIR))


def _load_module_with_replaced_imports(
    name: str,
    path: Path,
    import_replacements: dict[str, str] | None = None,
) -> ModuleType:
    """
    Load a module from a file path, optionally replacing import statements.

    Args:
        name: Name for the loaded module
        path: Path to the Python file
        import_replacements: Dict mapping old import statements to new ones
    """
    source = path.read_text()

    # Apply import replacements
    if import_replacements:
        for old, new in import_replacements.items():
            source = source.replace(old, new)

    # Create module
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__loader__ = None
    module.__package__ = ""

    # Execute the modified source
    code = compile(source, path, "exec")
    exec(code, module.__dict__)

    sys.modules[name] = module
    return module


# Load modules in dependency order
# github_client has no relative imports to other gateway modules
github_client = _load_module_with_replaced_imports(
    "github_client",
    GATEWAY_DIR / "github_client.py",
)

# policy imports from .github_client - convert to absolute
policy = _load_module_with_replaced_imports(
    "policy",
    GATEWAY_DIR / "policy.py",
    import_replacements={
        "from .github_client import": "from github_client import",
    },
)

# error_messages has no relative imports
error_messages = _load_module_with_replaced_imports(
    "error_messages",
    GATEWAY_DIR / "error_messages.py",
)

# repo_parser has no relative imports to other gateway modules
repo_parser = _load_module_with_replaced_imports(
    "repo_parser",
    GATEWAY_DIR / "repo_parser.py",
)

# repo_visibility has no relative imports to other gateway modules
repo_visibility = _load_module_with_replaced_imports(
    "repo_visibility",
    GATEWAY_DIR / "repo_visibility.py",
)

# private_repo_policy imports from repo_visibility, repo_parser, error_messages
private_repo_policy = _load_module_with_replaced_imports(
    "private_repo_policy",
    GATEWAY_DIR / "private_repo_policy.py",
    import_replacements={
        "from .repo_visibility import": "from repo_visibility import",
        "from .repo_parser import": "from repo_parser import",
        "from .error_messages import": "from error_messages import",
    },
)

# session_manager has no relative imports to other gateway modules
session_manager = _load_module_with_replaced_imports(
    "session_manager",
    GATEWAY_DIR / "session_manager.py",
)

# rate_limiter has no relative imports to other gateway modules
rate_limiter = _load_module_with_replaced_imports(
    "rate_limiter",
    GATEWAY_DIR / "rate_limiter.py",
)

# fork_policy imports from repo_visibility, repo_parser, private_repo_policy, error_messages
fork_policy = _load_module_with_replaced_imports(
    "fork_policy",
    GATEWAY_DIR / "fork_policy.py",
    import_replacements={
        "from .repo_visibility import": "from repo_visibility import",
        "from .repo_parser import": "from repo_parser import",
        "from .private_repo_policy import": "from private_repo_policy import",
        "from .error_messages import": "from error_messages import",
    },
)

# git_client has no relative imports to other gateway modules
git_client = _load_module_with_replaced_imports(
    "git_client",
    GATEWAY_DIR / "git_client.py",
)

# anthropic_credentials has no relative imports to other gateway modules
anthropic_credentials = _load_module_with_replaced_imports(
    "anthropic_credentials",
    GATEWAY_DIR / "anthropic_credentials.py",
)

# worktree_manager has no relative imports to other gateway modules
worktree_manager = _load_module_with_replaced_imports(
    "worktree_manager",
    GATEWAY_DIR / "worktree_manager.py",
)

# file_policy has no relative imports to other gateway modules
file_policy = _load_module_with_replaced_imports(
    "file_policy",
    GATEWAY_DIR / "file_policy.py",
)

# gateway imports from all
gateway = _load_module_with_replaced_imports(
    "gateway",
    GATEWAY_DIR / "gateway.py",
    import_replacements={
        "from .anthropic_credentials import": "from anthropic_credentials import",
        "from .git_client import": "from git_client import",
        "from .github_client import": "from github_client import",
        "from .policy import": "from policy import",
        "from .private_repo_policy import": "from private_repo_policy import",
        "from .repo_parser import": "from repo_parser import",
        "from .session_manager import": "from session_manager import",
        "from .rate_limiter import": "from rate_limiter import",
        "from .repo_visibility import": "from repo_visibility import",
        "from .worktree_manager import": "from worktree_manager import",
        "from .file_policy import": "from file_policy import",
    },
)

# Also load the __init__.py to prevent pytest from trying to import it
# and failing on relative imports
init_module = _load_module_with_replaced_imports(
    "gateway_init",
    GATEWAY_DIR / "__init__.py",
    import_replacements={
        "from .gateway import": "from gateway import",
        "from .github_client import": "from github_client import",
        "from .policy import": "from policy import",
    },
)
