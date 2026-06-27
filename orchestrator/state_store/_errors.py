"""Exception types + pipeline-id validation for the state store (#3312).

Extracted verbatim from the pre-split ``state_store.py`` as part of the
slice-3 decomposition. The barrel re-exports every symbol here, so importers
and ``unittest.mock.patch`` targets keep resolving through
``state_store.<name>``.
"""

import re

# Valid pipeline ID formats:
#   issue-{number}[-qualifier[-...]]  — GitHub issue-driven
#   {LETTERS}-{digits}[-qualifier[-...]] — JIRA ticket-driven (e.g. PROJ-1234, PROJ-1234-v2-hotfix)
#   local-{8 hex chars}         — local dev
#   pipeline-{8 hex chars}      — auto-generated
PIPELINE_ID_PATTERN = re.compile(
    r"^("
    r"issue-[0-9]+(-[a-z0-9]+)*"
    r"|[A-Z][A-Z0-9]+-[0-9]+(-[a-z0-9]+)*"
    r"|local-[0-9a-f]{8}"
    r"|pipeline-[0-9a-f]{8}"
    r")$"
)


class StateStoreError(Exception):
    """Base exception for state store errors."""

    pass


class PipelineNotFoundError(StateStoreError):
    """Pipeline state not found."""

    pass


class StateValidationError(StateStoreError):
    """Pipeline state validation failed."""

    pass


class GitOperationError(StateStoreError):
    """Git operation failed."""

    pass


class InvalidPipelineIdError(StateStoreError):
    """Invalid pipeline ID format."""

    pass


class VersionConflictError(StateStoreError):
    """Optimistic locking version conflict."""

    pass


def _validate_pipeline_id(pipeline_id: str) -> None:
    """Validate pipeline ID format to prevent path traversal attacks.

    Args:
        pipeline_id: Pipeline ID to validate

    Raises:
        InvalidPipelineIdError: If pipeline ID format is invalid
    """
    if not pipeline_id or not PIPELINE_ID_PATTERN.match(pipeline_id):
        raise InvalidPipelineIdError(f"Invalid pipeline ID format: {pipeline_id}")


# Public alias: ``_validate_pipeline_id`` predates the cross-module callers in
# ``routes/__init__.py``. Underscore-prefixed names should stay internal, so
# importers (production code, tests) can use ``validate_pipeline_id`` instead.
validate_pipeline_id = _validate_pipeline_id
