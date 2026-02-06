"""
Custom assertions for common test patterns.

These assertions provide clear, descriptive failure messages and reduce
boilerplate in tests.
"""

from typing import Any


def assert_allowed(result: Any, *, message_contains: str | None = None) -> None:
    """Assert that a PolicyResult indicates an allowed operation.

    Args:
        result: PolicyResult object to check
        message_contains: Optional substring that should appear in the reason

    Raises:
        AssertionError: If the operation was blocked or message doesn't match.
    """
    assert result.allowed, (
        f"Expected operation to be allowed but was blocked.\n"
        f"Reason: {result.reason}\n"
        f"Details: {result.details}"
    )

    if message_contains is not None:
        assert message_contains.lower() in result.reason.lower(), (
            f"Expected reason to contain '{message_contains}'.\n"
            f"Actual reason: {result.reason}"
        )


def assert_blocked(
    result: Any,
    *,
    message_contains: str | None = None,
    detail_key: str | None = None,
    detail_value: Any = None,
) -> None:
    """Assert that a PolicyResult indicates a blocked operation.

    Args:
        result: PolicyResult object to check
        message_contains: Optional substring that should appear in the reason
        detail_key: Optional key that should exist in details
        detail_value: Optional value that detail_key should have

    Raises:
        AssertionError: If the operation was allowed or message doesn't match.
    """
    assert not result.allowed, (
        f"Expected operation to be blocked but was allowed.\n"
        f"Reason: {result.reason}\n"
        f"Details: {result.details}"
    )

    if message_contains is not None:
        assert message_contains.lower() in result.reason.lower(), (
            f"Expected reason to contain '{message_contains}'.\n"
            f"Actual reason: {result.reason}"
        )

    if detail_key is not None:
        assert result.details is not None, "Expected details to be set but was None"
        assert detail_key in result.details, (
            f"Expected details to contain key '{detail_key}'.\n"
            f"Actual details: {result.details}"
        )
        if detail_value is not None:
            assert result.details[detail_key] == detail_value, (
                f"Expected details['{detail_key}'] to be {detail_value!r}.\n"
                f"Actual value: {result.details[detail_key]!r}"
            )


def assert_session_valid(result: Any, *, mode: str | None = None) -> None:
    """Assert that a SessionValidationResult indicates a valid session.

    Args:
        result: SessionValidationResult object to check
        mode: Optional expected session mode ("private" or "public")

    Raises:
        AssertionError: If the session is invalid or mode doesn't match.
    """
    assert result.valid, (
        f"Expected session to be valid but was invalid.\n" f"Error: {result.error}"
    )

    assert result.session is not None, "Expected session to be set but was None"

    if mode is not None:
        assert result.session.mode == mode, (
            f"Expected session mode to be '{mode}'.\n"
            f"Actual mode: {result.session.mode}"
        )


def assert_session_invalid(result: Any, *, error_contains: str | None = None) -> None:
    """Assert that a SessionValidationResult indicates an invalid session.

    Args:
        result: SessionValidationResult object to check
        error_contains: Optional substring that should appear in the error

    Raises:
        AssertionError: If the session is valid or error doesn't match.
    """
    assert not result.valid, (
        f"Expected session to be invalid but was valid.\n"
        f"Session: container_id={result.session.container_id if result.session else None}"
    )

    if error_contains is not None:
        assert result.error is not None, "Expected error message but was None"
        assert error_contains.lower() in result.error.lower(), (
            f"Expected error to contain '{error_contains}'.\n"
            f"Actual error: {result.error}"
        )
