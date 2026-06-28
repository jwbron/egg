"""Gateway HTTP request helper and legacy error renderer.

Extracted verbatim from the monolithic ``contract_cli.py`` (#3312,
slice-1). No behaviour change.
"""

import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ._config import get_gateway_url, get_session_token
from ._errors import GatewayError


def make_gateway_request(
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a request to the gateway API.

    Args:
        endpoint: API endpoint (e.g., "/api/v1/contract/123")
        method: HTTP method
        data: Request body data (for POST requests)

    Returns:
        Response data as dictionary

    Raises:
        GatewayError: On HTTP/URL/timeout failure.  The caller (either a
            ``cmd_*`` shim or a pure handler in ``egg_agent_tools``) is
            responsible for rendering the error — ``make_gateway_request``
            itself no longer calls ``sys.exit``.  Callers that want the
            legacy print-and-exit behaviour should catch ``GatewayError``
            and call :func:`_render_gateway_error_and_exit`.
    """
    gateway_url = get_gateway_url()
    url = f"{gateway_url}{endpoint}"

    headers = {"Content-Type": "application/json"}

    # Add session token if available
    token = get_session_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(data).encode() if data else None

    try:
        request = Request(url, data=body, headers=headers, method=method)
        with urlopen(request, timeout=30) as response:
            result: dict[str, Any] = json.loads(response.read().decode())
            return result
    except HTTPError as e:
        try:
            error_data = json.loads(e.read().decode())
            message = error_data.get("message", str(e))
            details = error_data.get("details") or {}
        except json.JSONDecodeError, Exception:
            message = str(e)
            details = {}
        raise GatewayError(
            message,
            status_code=getattr(e, "code", None),
            details=details if isinstance(details, dict) else {"raw": details},
        ) from e
    except URLError as e:
        raise GatewayError(
            f"connecting to gateway: {e.reason}",
            hint="Is the gateway running?",
        ) from e
    except TimeoutError as e:
        raise GatewayError("Request to gateway timed out") from e


def _render_gateway_error_and_exit(err: GatewayError) -> int:
    """Render a GatewayError on stderr in the legacy make_gateway_request shape.

    Kept as a helper so cmd_* shims that historically exited from inside
    ``make_gateway_request`` continue to produce byte-identical output.
    """
    # Legacy shape — message first, then optional indented details, then
    # the hint (for URL errors).
    print(f"Error: {err.message}", file=sys.stderr)
    if err.details:
        try:
            print(f"Details: {json.dumps(err.details, indent=2)}", file=sys.stderr)
        except TypeError, ValueError:
            pass
    if err.hint:
        print(err.hint, file=sys.stderr)
    return err.exit_code
