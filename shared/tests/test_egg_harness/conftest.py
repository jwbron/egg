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


@pytest.fixture
def multi_turn_conversation():
    """Multi-turn conversation with tool_use and tool_result messages."""
    return [
        {"role": "user", "content": "Please read the config file."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll read the config file."},
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "Read",
                    "input": {"file_path": "/tmp/config.json"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_001",
                    "content": '{"key": "value"}',
                },
            ],
        },
        {
            "role": "assistant",
            "content": "The config contains key=value. I decided to use the default approach.",
        },
        {"role": "user", "content": "Now write the output file."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Writing the output file now."},
                {
                    "type": "tool_use",
                    "id": "tu_002",
                    "name": "Write",
                    "input": {"file_path": "/tmp/output.txt", "content": "done"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_002",
                    "content": "File written successfully.",
                },
            ],
        },
        {"role": "assistant", "content": "Done! The output file has been written."},
    ]
