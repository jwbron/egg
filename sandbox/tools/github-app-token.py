#!/usr/bin/env python3
"""
Generate GitHub App installation access token.

This script generates a short-lived (1 hour) installation token from GitHub App
credentials. Used by the egg launcher to authenticate the container's gh CLI and git push.

Usage:
    github-app-token.py [--config-dir DIR]

Credentials are read from:
    ~/.config/egg/secrets.env             - Contains GITHUB_APP_ID and GITHUB_APP_INSTALLATION_ID
    ~/.config/egg/github-app.pem          - Private key file

Output:
    Prints the installation access token to stdout (for capture by egg script)
    Exit code 0 on success, non-zero on failure

Token expires in 1 hour. Generate fresh token for each container launch.
"""

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def create_jwt(app_id: str, private_key: str) -> str:
    """
    Create a JSON Web Token (JWT) for GitHub App authentication.

    Uses cryptography library for RS256 signing.
    The JWT is valid for 10 minutes (GitHub's maximum).
    """

    def b64url_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    # JWT Header
    header = {"alg": "RS256", "typ": "JWT"}
    header_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode())

    # JWT Payload
    now = int(time.time())
    payload = {
        "iat": now - 60,  # Issued 60 seconds ago (account for clock skew)
        "exp": now + 600,  # Expires in 10 minutes (GitHub maximum)
        "iss": app_id,
    }
    payload_b64 = b64url_encode(json.dumps(payload, separators=(",", ":")).encode())

    # Message to sign
    message = f"{header_b64}.{payload_b64}".encode()

    # Load private key and sign
    private_key_obj = serialization.load_pem_private_key(
        private_key.encode(), password=None, backend=default_backend()  # type: ignore[no-untyped-call]
    )

    signature = private_key_obj.sign(message, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[call-arg,arg-type]

    signature_b64 = b64url_encode(signature)

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def get_installation_token(jwt_token: str, installation_id: str) -> tuple[str | None, str | None]:
    """
    Exchange JWT for an installation access token.

    Returns:
        Tuple of (token, error_message). Token is None on failure.
    """
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"

    request = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "egg",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode())
            return data.get("token"), None
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return None, f"HTTP {e.code}: {error_body}"
    except urllib.error.URLError as e:
        return None, f"URL Error: {e.reason}"
    except Exception as e:
        return None, str(e)


def _read_secrets_env(secrets_file: Path) -> dict[str, str]:
    """Read secrets.env file into a dictionary."""
    secrets_dict: dict[str, str] = {}
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                secrets_dict[key.strip()] = value.strip()
    return secrets_dict


def load_config(config_dir: Path) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Load GitHub App configuration from secrets.env and .pem file.

    Returns:
        Tuple of (app_id, installation_id, private_key, error_message)
    """
    secrets_file = config_dir / "secrets.env"
    private_key_file = config_dir / "github-app.pem"

    # Read secrets from secrets.env
    secrets = _read_secrets_env(secrets_file)
    app_id = secrets.get("GITHUB_APP_ID")
    installation_id = secrets.get("GITHUB_APP_INSTALLATION_ID")

    # Check required values
    missing = []
    if not app_id:
        missing.append("GITHUB_APP_ID in secrets.env")
    if not installation_id:
        missing.append("GITHUB_APP_INSTALLATION_ID in secrets.env")
    if not private_key_file.exists():
        missing.append(str(private_key_file))

    if missing:
        return None, None, None, f"Missing config: {', '.join(missing)}"

    # At this point app_id and installation_id are guaranteed non-None
    assert app_id is not None
    assert installation_id is not None

    try:
        private_key = private_key_file.read_text()

        # Validate
        if not app_id.isdigit():
            return None, None, None, f"Invalid app_id (not numeric): {app_id}"
        if not installation_id.isdigit():
            return None, None, None, f"Invalid installation_id (not numeric): {installation_id}"
        if "PRIVATE KEY" not in private_key:
            return None, None, None, "Invalid private key (doesn't look like PEM format)"

        return app_id, installation_id, private_key, None

    except Exception as e:
        return None, None, None, f"Failed to read config: {e}"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate GitHub App installation access token")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path.home() / ".config" / "egg",
        help="Directory containing GitHub App config files",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress error messages (just exit non-zero on failure)",
    )

    args = parser.parse_args()

    # Load configuration
    app_id, installation_id, private_key, error = load_config(args.config_dir)
    if error:
        if not args.quiet:
            print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

    # At this point, error is None so all three values are set
    assert app_id is not None
    assert installation_id is not None
    assert private_key is not None

    # Generate JWT
    try:
        jwt_token = create_jwt(app_id, private_key)
    except Exception as e:
        if not args.quiet:
            print(f"ERROR: Failed to create JWT: {e}", file=sys.stderr)
        sys.exit(1)

    # Exchange for installation token
    token, error = get_installation_token(jwt_token, installation_id)
    if error:
        if not args.quiet:
            print(f"ERROR: Failed to get installation token: {error}", file=sys.stderr)
        sys.exit(1)

    # Output token
    print(token)
    sys.exit(0)


if __name__ == "__main__":
    main()
