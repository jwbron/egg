"""Shared fixtures for egg_harness tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_messages():
    """Sample conversation messages for provider tests."""
    return [
        {"role": "user", "content": "Hello, how are you?"},
    ]


@pytest.fixture
def sample_tools():
    """Sample tool definitions for provider/registry tests."""
    return [
        {
            "name": "Bash",
            "description": "Execute a bash command",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to execute"},
                },
                "required": ["command"],
            },
        },
        {
            "name": "Read",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to file"},
                },
                "required": ["file_path"],
            },
        },
    ]


@pytest.fixture
def sample_system_prompt():
    """Sample system prompt for provider tests."""
    return "You are a helpful assistant."
