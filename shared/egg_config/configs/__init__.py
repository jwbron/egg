"""
Service-specific configuration classes.

Each config class implements the BaseConfig interface, providing:
- Validation of configuration values
- Health checks for service connectivity
- Safe serialization with secret masking
- Loading from environment variables and config files

Available configs:
- GitHubConfig: GitHub authentication tokens
- LLMConfig: LLM provider API keys
- GatewayConfig: Gateway sidecar settings
"""

from .gateway import GatewayConfig
from .github import GitHubConfig
from .llm import LLMConfig

__all__ = [
    "GatewayConfig",
    "GitHubConfig",
    "LLMConfig",
]
