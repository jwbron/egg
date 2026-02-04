#!/usr/bin/env python3
"""
Configuration Validation Script

Validates egg configuration files and tests API connectivity.

Usage:
    ./scripts/validate-config.py           # Validate config loads correctly
    ./scripts/validate-config.py --health  # Also test API connectivity
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def verify_config(run_health_checks: bool = False) -> bool:
    """Verify configuration by testing config loading and optionally API connectivity."""
    print("\n" + "=" * 60)
    print(" CONFIGURATION VALIDATION")
    print("=" * 60)

    # Add the repo's shared directory to path
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    sys.path.insert(0, str(repo_root / "shared"))

    try:
        from egg_config import (
            ConfluenceConfig,
            GatewayConfig,
            GitHubConfig,
            JiraConfig,
            SlackConfig,
        )

        errors = []

        # Test SlackConfig
        slack = SlackConfig.from_env()
        validation = slack.validate()
        if validation.is_valid:
            print("✓ SlackConfig loads successfully")
            print(f"    channel: {slack.channel or '(not set)'}")
            print(f"    bot_token: {'set' if slack.bot_token else 'NOT SET'}")
        else:
            errors.extend(validation.errors)
            print(f"✗ SlackConfig validation errors: {validation.errors}")

        # Test GitHubConfig
        github = GitHubConfig.from_env()
        validation = github.validate()
        if validation.is_valid:
            print("✓ GitHubConfig loads successfully")
            print(
                f"    token: {'set' if github.token else 'NOT SET'} (source: {github._token_source or 'none'})"
            )
        # GitHub might not be configured, that's OK
        elif github.token:
            errors.extend(validation.errors)
            print(f"✗ GitHubConfig validation errors: {validation.errors}")
        else:
            print("⚠ GitHubConfig: No token configured")

        # Test GatewayConfig
        gateway = GatewayConfig.from_env()
        print("✓ GatewayConfig loads successfully")
        print(
            f"    secret: {'set' if gateway.secret else 'NOT SET'} (source: {gateway._secret_source or 'none'})"
        )

        # Test JiraConfig
        jira = JiraConfig.from_env()
        validation = jira.validate()
        if validation.is_valid:
            print("✓ JiraConfig loads successfully")
            print(f"    base_url: {jira.base_url or '(not set)'}")
        elif jira.base_url:
            errors.extend(validation.errors)
            print(f"✗ JiraConfig validation errors: {validation.errors}")
        else:
            print("⚠ JiraConfig: Not configured")

        # Test ConfluenceConfig
        confluence = ConfluenceConfig.from_env()
        validation = confluence.validate()
        if validation.is_valid:
            print("✓ ConfluenceConfig loads successfully")
            print(f"    base_url: {confluence.base_url or '(not set)'}")
        elif confluence.base_url:
            errors.extend(validation.errors)
            print(f"✗ ConfluenceConfig validation errors: {validation.errors}")
        else:
            print("⚠ ConfluenceConfig: Not configured")

        if errors:
            print("\n⚠ Some validation errors occurred. Check your configuration.")
            return False
        else:
            print("\n✓ All configurations loaded and validated successfully!")

        # Run health checks if requested
        if run_health_checks:
            run_api_health_checks(slack, github, jira, confluence)

        return True

    except Exception as e:
        print(f"\n✗ Validation failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def run_api_health_checks(slack, github, jira, confluence):
    """Run actual API connectivity tests."""
    print("\n" + "=" * 60)
    print(" HEALTH CHECKS (API Connectivity)")
    print("=" * 60)

    # =========================================================================
    # Slack Health Checks
    # =========================================================================
    print("\n--- Slack ---")

    # Test bot token (xoxb-)
    if slack.bot_token:
        result = slack.health_check(timeout=10.0)
        if result.healthy:
            latency = f" ({result.latency_ms:.0f}ms)" if result.latency_ms else ""
            print(f"  ✓ Bot token (xoxb-): {result.message}{latency}")
        else:
            print(f"  ✗ Bot token (xoxb-): {result.message}")
    else:
        print("  ⚠ Bot token: Not configured")

    # Test app token (xapp-) for Socket Mode
    if slack.app_token:
        result = check_slack_app_token(slack.app_token)
        if result["healthy"]:
            print("  ✓ App token (xapp-): Valid for Socket Mode")
        else:
            print(f"  ✗ App token (xapp-): {result['message']}")
    else:
        print("  ⚠ App token: Not configured (needed for Socket Mode)")

    # =========================================================================
    # GitHub Health Checks
    # =========================================================================
    print("\n--- GitHub ---")

    def test_github_token(token, name, timeout=10.0):
        """Test a GitHub token and return result."""
        if not token:
            return {"healthy": False, "message": "Not configured", "skipped": True}

        try:
            start = time.time()
            req = urllib.request.Request(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "egg-config/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode())
                latency = (time.time() - start) * 1000
                login = data.get("login", "unknown")
                return {
                    "healthy": True,
                    "message": f"Authenticated as {login}",
                    "latency_ms": latency,
                    "login": login,
                }
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return {"healthy": False, "message": "Invalid or expired"}
            return {"healthy": False, "message": f"HTTP {e.code}"}
        except Exception as e:
            return {"healthy": False, "message": str(e)}

    # Test primary token
    result = test_github_token(github.token, "Primary")
    if result.get("skipped"):
        print("  ⚠ Primary token: Not configured")
    elif result["healthy"]:
        latency = f" ({result['latency_ms']:.0f}ms)" if result.get("latency_ms") else ""
        print(f"  ✓ Primary token: {result['message']}{latency}")
    else:
        print(f"  ✗ Primary token: {result['message']}")

    # Test readonly token
    result = test_github_token(github.readonly_token, "Readonly")
    if result.get("skipped"):
        print("  ⚠ Readonly token: Not configured")
    elif result["healthy"]:
        latency = f" ({result['latency_ms']:.0f}ms)" if result.get("latency_ms") else ""
        print(f"  ✓ Readonly token: {result['message']}{latency}")
    else:
        print(f"  ✗ Readonly token: {result['message']}")

    # Test incognito token
    result = test_github_token(github.incognito_token, "Incognito")
    if result.get("skipped"):
        print("  ⚠ Incognito token: Not configured")
    elif result["healthy"]:
        latency = f" ({result['latency_ms']:.0f}ms)" if result.get("latency_ms") else ""
        print(f"  ✓ Incognito token: {result['message']}{latency}")
    else:
        print(f"  ✗ Incognito token: {result['message']}")

    # =========================================================================
    # JIRA Health Check
    # =========================================================================
    print("\n--- JIRA ---")

    if jira.base_url and jira.api_token:
        result = jira.health_check(timeout=10.0)
        if result.healthy:
            latency = f" ({result.latency_ms:.0f}ms)" if result.latency_ms else ""
            print(f"  ✓ API: {result.message}{latency}")
        else:
            print(f"  ✗ API: {result.message}")
    else:
        missing = []
        if not jira.base_url:
            missing.append("JIRA_BASE_URL")
        if not jira.api_token:
            missing.append("JIRA_API_TOKEN")
        print(f"  ⚠ Not configured (missing: {', '.join(missing)})")

    # =========================================================================
    # Confluence Health Check
    # =========================================================================
    print("\n--- Confluence ---")

    if confluence.base_url and confluence.api_token:
        result = confluence.health_check(timeout=10.0)
        if result.healthy:
            latency = f" ({result.latency_ms:.0f}ms)" if result.latency_ms else ""
            print(f"  ✓ API: {result.message}{latency}")
        else:
            print(f"  ✗ API: {result.message}")
    else:
        missing = []
        if not confluence.base_url:
            missing.append("CONFLUENCE_BASE_URL")
        if not confluence.api_token:
            missing.append("CONFLUENCE_API_TOKEN")
        print(f"  ⚠ Not configured (missing: {', '.join(missing)})")


def check_slack_app_token(app_token: str, timeout: float = 10.0) -> dict:
    """Test Slack app token for Socket Mode connectivity."""
    try:
        # Use apps.connections.open to verify app token
        req = urllib.request.Request(
            "https://slack.com/api/apps.connections.open",
            headers={
                "Authorization": f"Bearer {app_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode())
            if data.get("ok"):
                return {"healthy": True, "message": "Valid"}
            else:
                error = data.get("error", "unknown error")
                return {"healthy": False, "message": error}
    except Exception as e:
        return {"healthy": False, "message": str(e)}


def main():
    parser = argparse.ArgumentParser(
        description="Validate egg configuration files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s           Validate configuration files load correctly
  %(prog)s --health  Also test API connectivity (Slack, GitHub, JIRA, Confluence)
""",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Run API connectivity tests after validation",
    )

    args = parser.parse_args()

    success = verify_config(run_health_checks=args.health)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
