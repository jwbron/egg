"""
Git Client - Wraps git CLI with validation and credential management.

Provides:
- Path validation (prevent traversal attacks)
- Argument validation with per-operation allowlists
- Credential helper management for authenticated git operations

Decomposition note (#3312 slice-11): the pre-split single-file module is
now a sub-package. This ``__init__.py`` is the **stable public API
barrel** — every externally-referenced and ``unittest.mock.patch``-target
symbol re-exports here so ``from git_client import git_cmd`` /
``from gateway.git_client import validate_repo_path`` and
``patch("git_client.os.path.realpath")`` keep resolving unchanged. The
implementation lives in underscore-prefixed private submodules:

- ``_remote``        — remote-URL helpers + the validated ``git`` argv builder
- ``_policy``        — static validation tables (``GIT_ALLOWED_COMMANDS`` …)
- ``_validation``    — repo-path + git-argument validation logic
- ``_credentials``   — credential-helper lifecycle + per-repo token resolution
- ``_push_analysis`` — changed-files detection for a push range
- ``_attribution``   — per-file push attribution (commit -> author role)
- ``_branch_ops``    — branch-switch / reset detection + ``rebase --onto`` builder

Pure refactor: every re-exported symbol is AST-identical to its
pre-split definition.
"""

import os  # noqa: F401  # re-exported as the ``git_client.os`` patch seam (test_git_validation patches ``git_client.os.path.realpath``)
import sys
from pathlib import Path

# Add shared directory to path for egg_logging (one level deeper than the
# pre-split git_client.py, hence the extra ``.parent``).
_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))

# Import repo_config for auth mode support (likewise one level deeper).
_config_path = Path(__file__).parent.parent.parent / "config"
if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))

from ._attribution import (
    INFRA_ATTRIBUTION_ROLE,
    INFRA_COMMITTER_EMAILS,
    AttributedFile,
    AttributedPushRange,
    _committer_email_for_commit,
    _enumerate_push_commits,
    _files_for_commit,
    _patch_ids_for_commits,
    get_attributed_changed_files_in_push,
)
from ._branch_ops import (
    build_rebase_onto_args,
    extract_reset_target_ref,
    is_branch_switch,
)
from ._credentials import (
    _ASKPASS_SCRIPT,
    cleanup_credential_helper,
    create_credential_helper,
    get_token_for_repo,
)
from ._policy import (
    _CHECKOUT_FILE_FLAGS,
    ALLOWED_FLAG_VALUES,
    ALLOWED_REPO_PATHS,
    BLOCKED_GIT_FLAGS,
    FLAG_NORMALIZATION,
    GIT_ALLOWED_COMMANDS,
    REPOS_PARENT_DIRECTORIES,
)
from ._push_analysis import (
    _SHA_LINE_RE,
    _fallback_base_candidates,
    _fetch_base_branch_best_effort,
    _parse_sha_lines,
    get_changed_files_in_push,
)
from ._remote import (
    GIT_CLI,
    get_authenticated_remote_target,
    git_cmd,
    is_ssh_url,
    is_url_remote,
    resolve_remote_url,
    ssh_url_to_https,
)
from ._validation import (
    is_branch_switching_checkout,
    is_branch_switching_operation,
    is_repos_parent_directory,
    normalize_flag,
    validate_git_args,
    validate_repo_path,
)

__all__ = [
    # _remote
    "GIT_CLI",
    "git_cmd",
    "ssh_url_to_https",
    "is_ssh_url",
    "is_url_remote",
    "resolve_remote_url",
    "get_authenticated_remote_target",
    # _policy
    "ALLOWED_REPO_PATHS",
    "REPOS_PARENT_DIRECTORIES",
    "BLOCKED_GIT_FLAGS",
    "ALLOWED_FLAG_VALUES",
    "GIT_ALLOWED_COMMANDS",
    "FLAG_NORMALIZATION",
    "_CHECKOUT_FILE_FLAGS",
    # _validation
    "is_repos_parent_directory",
    "validate_repo_path",
    "normalize_flag",
    "validate_git_args",
    "is_branch_switching_checkout",
    "is_branch_switching_operation",
    # _credentials
    "_ASKPASS_SCRIPT",
    "create_credential_helper",
    "cleanup_credential_helper",
    "get_token_for_repo",
    # _push_analysis
    "_SHA_LINE_RE",
    "_parse_sha_lines",
    "_fetch_base_branch_best_effort",
    "_fallback_base_candidates",
    "get_changed_files_in_push",
    # _attribution
    "AttributedFile",
    "AttributedPushRange",
    "_enumerate_push_commits",
    "_files_for_commit",
    "_patch_ids_for_commits",
    "INFRA_ATTRIBUTION_ROLE",
    "INFRA_COMMITTER_EMAILS",
    "_committer_email_for_commit",
    "get_attributed_changed_files_in_push",
    # _branch_ops
    "is_branch_switch",
    "extract_reset_target_ref",
    "build_rebase_onto_args",
]
