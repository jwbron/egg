#!/usr/bin/env python3
"""
Configuration Validation Script

Validates egg configuration files and tests API connectivity.

Usage:
    ./scripts/validate-config.py           # Validate config loads correctly
    ./scripts/validate-config.py --health  # Also test API connectivity
"""

import argparse
import sys
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
            GatewayConfig,
            GitHubConfig,
            LLMConfig,
        )

        errors = []

        # Test GitHubConfig
        github = GitHubConfig.from_env()
        validation = github.validate()
        if validation.is_valid:
            print("✓ GitHubConfig loads successfully")
            print(f"    token: {'set' if github.token else 'NOT SET'}")
        elif github.token:
            errors.extend(validation.errors)
            print(f"✗ GitHubConfig validation errors: {validation.errors}")
        else:
            print("⚠ GitHubConfig: No token configured")

        # Test GatewayConfig
        gateway = GatewayConfig.from_env()
        validation = gateway.validate()
        if validation.is_valid:
            print("✓ GatewayConfig loads successfully")
            print(f"    secret: {'set' if gateway.secret else 'NOT SET'}")
        else:
            errors.extend(validation.errors)
            print(f"✗ GatewayConfig validation errors: {validation.errors}")

        # Test LLMConfig
        llm = LLMConfig.from_env()
        validation = llm.validate()
        if validation.is_valid:
            print("✓ LLMConfig loads successfully")
            print(f"    model: {llm.model or '(not set)'}")
        elif llm.anthropic_api_key:
            errors.extend(validation.errors)
            print(f"✗ LLMConfig validation errors: {validation.errors}")
        else:
            print("⚠ LLMConfig: No API key configured")

        if errors:
            print("\n⚠ Some validation errors occurred. Check your configuration.")
            return False
        else:
            print("\n✓ All configurations loaded and validated successfully!")

        # Run health checks if requested
        if run_health_checks:
            run_api_health_checks(github, gateway, llm)

        return True

    except Exception as e:
        print(f"\n✗ Validation failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def run_api_health_checks(github, gateway, llm):
    """Run actual API connectivity tests."""
    print("\n" + "=" * 60)
    print(" HEALTH CHECKS (API Connectivity)")
    print("=" * 60)

    # =========================================================================
    # GitHub Health Check
    # =========================================================================
    print("\n--- GitHub ---")

    result = github.health_check(timeout=10.0)
    if result.healthy:
        latency = f" ({result.latency_ms:.0f}ms)" if result.latency_ms else ""
        print(f"  ✓ API: {result.message}{latency}")
    else:
        print(f"  ✗ API: {result.message}")

    # =========================================================================
    # Gateway Health Check
    # =========================================================================
    print("\n--- Gateway ---")

    result = gateway.health_check(timeout=10.0)
    if result.healthy:
        latency = f" ({result.latency_ms:.0f}ms)" if result.latency_ms else ""
        print(f"  ✓ API: {result.message}{latency}")
    else:
        print(f"  ✗ API: {result.message}")

    # =========================================================================
    # LLM Health Check
    # =========================================================================
    print("\n--- LLM ---")

    result = llm.health_check(timeout=10.0)
    if result.healthy:
        latency = f" ({result.latency_ms:.0f}ms)" if result.latency_ms else ""
        print(f"  ✓ API: {result.message}{latency}")
    else:
        print(f"  ✗ API: {result.message}")


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
