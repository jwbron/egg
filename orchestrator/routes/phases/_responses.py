"""Shared JSON response builders for the phases blueprint (#3312 decomposition)."""

from typing import Any

from flask import Response, jsonify


def make_error_response(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
    reason: str | None = None,
) -> tuple[Response, int]:
    """Create an error response.

    ``reason`` is a stable, machine-readable enum-like code that disambiguates
    responses sharing the same HTTP status (especially 409, where distinct
    gates — health checks vs. unresolved HITL — would otherwise collapse into
    one signal). Callers should switch on ``reason`` rather than parsing
    ``message``. See #1939.
    """
    response: dict[str, Any] = {"success": False, "message": message}
    if reason is not None:
        response["reason"] = reason
    if details is not None:
        response["details"] = details
    return jsonify(response), status_code


def make_success_response(
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200
