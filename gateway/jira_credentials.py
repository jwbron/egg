"""
Jira Credentials Manager for Gateway Sidecar.

Thin compatibility shim that re-exports the canonical credential surface
from :mod:`egg_jira_credentials` (under ``shared/``).  The shared module
is the single source of truth for Atlassian Cloud credential loading
across the gateway sidecar AND the orchestrator-side
:mod:`orchestrator.jira_transitions` client (#1557 TASK-1-5,
risk_analyst R1 mitigation).

Existing gateway-side imports continue to resolve unchanged:

.. code-block:: python

    from gateway.jira_credentials import (
        JiraCredentials,
        JiraCredentialsUnavailable,
        get_jira_credentials,
    )

Credential precedence (decision F1, issue #1931) and secrets-file path
semantics live in the shared module — see that module's docstring for
the full surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add shared directory to path so ``egg_jira_credentials`` resolves.
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_jira_credentials import (  # noqa: E402 — imports after sys.path setup
    SECRETS_PATH,
    JiraCredentials,
    JiraCredentialsManager,
    JiraCredentialsUnavailable,
    get_jira_credentials,
    get_jira_credentials_manager,
    parse_env_file,
)


def reload_jira_credentials() -> None:
    """Force the singleton credentials manager to reload on next access.

    Wraps :meth:`JiraCredentialsManager.reload` for legacy callers that
    historically did ``from gateway.jira_credentials import
    reload_jira_credentials``.
    """
    get_jira_credentials_manager().reload()


__all__ = [
    "JiraCredentials",
    "JiraCredentialsManager",
    "JiraCredentialsUnavailable",
    "SECRETS_PATH",
    "get_jira_credentials",
    "get_jira_credentials_manager",
    "parse_env_file",
    "reload_jira_credentials",
]
