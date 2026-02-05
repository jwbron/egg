"""
Claude Code runner using headless mode (subprocess).

This module provides functions for running Claude via the Claude Code CLI
in headless mode (`claude --print`), which provides full access to tools
and filesystem.

Supports both synchronous and asynchronous execution with streaming output.
"""

import asyncio
import contextlib
import json
import logging
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from llm.claude.config import ClaudeConfig
from llm.result import AgentResult

logger = logging.getLogger(__name__)

# Default model for sandbox agents
# Using the alias 'opus' which maps to the latest Opus model (claude-opus-4-5-*)
DEFAULT_MODEL = "opus"

# Minimum known-good Claude Code version (for version check logging)
MIN_CLAUDE_VERSION = "1.0.0"

# Read chunk size for stream processing (1MB chunks allow handling large outputs)
_READ_CHUNK_SIZE = 1024 * 1024

# Buffer size warning threshold (50MB) - log warning if buffer grows this large
# without seeing a newline, which could indicate malformed output
_BUFFER_WARNING_THRESHOLD = 50 * 1024 * 1024


def _check_claude_version() -> str | None:
    """Check Claude Code CLI version and log warning if too old.

    Returns:
        Version string if available, None otherwise.
    """
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            version = result.stdout.strip().split()[0] if result.stdout else None
            if version:
                logger.debug(f"Claude Code version: {version}")
                return version
    except Exception as e:
        logger.warning(f"Could not check Claude Code version: {e}")
    return None


# Perform version check at module import
_claude_version = _check_claude_version()


def _classify_error(returncode: int, stderr: str) -> str:
    """Map subprocess failure to error category.

    Args:
        returncode: Process exit code
        stderr: Standard error output

    Returns:
        Human-readable error message
    """
    stderr_lower = stderr.lower()

    if "invalid_api_key" in stderr_lower or "authentication" in stderr_lower:
        return "Authentication failed"
    if "rate_limit" in stderr_lower or "429" in stderr_lower:
        return "Rate limited"
    if "model" in stderr_lower and "not found" in stderr_lower:
        return "Model not available"
    if "permission" in stderr_lower:
        return "Permission denied"

    return stderr[:500] if stderr else f"Exit code {returncode}"


def _extract_text_from_event(event: dict[str, Any]) -> str | None:
    """Extract text content from a stream-json event.

    Args:
        event: Parsed JSON event from stream-json output

    Returns:
        Text content if present, None otherwise
    """
    event_type = event.get("type")

    # Assistant message contains content blocks
    if event_type == "assistant":
        message = event.get("message", {})
        content = message.get("content", [])
        texts = []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if text:
                    texts.append(text)
            elif block_type == "thinking":
                thinking = block.get("thinking")
                if thinking and thinking != "(no content)":
                    texts.append(f"[Thinking: {thinking}]")
        if texts:
            return "\n".join(texts)

    # Result message (final)
    if event_type == "result":
        result = event.get("result")
        if result:
            return str(result)

    return None


def _extract_model_from_event(event: dict[str, Any], current_model: str | None) -> str | None:
    """Extract model information from a stream-json event.

    Args:
        event: Parsed JSON event
        current_model: Previously extracted model (returned if no new info)

    Returns:
        Model ID string or None
    """
    event_type = event.get("type")

    # System init contains resolved model
    if event_type == "system" and event.get("subtype") == "init":
        model = event.get("model")
        if model:
            logger.debug(f"Model from init event: {model}")
            return str(model)

    # Assistant message has model in nested message
    if event_type == "assistant":
        message = event.get("message", {})
        model = message.get("model")
        if model and model != current_model:
            logger.debug(f"Model from assistant event: {model}")
            return str(model)

    return current_model


async def _read_lines_unbuffered(
    stream: asyncio.StreamReader,
) -> AsyncIterator[bytes]:
    """Read lines from a stream without the default 64KB buffer limit.

    Python's asyncio StreamReader has a default limit of 64KB per line.
    When Claude Code outputs large JSON events (e.g., thinking blocks with
    extensive content), a single line can exceed this limit, causing:
    "Separator is found, but chunk is longer than limit"

    This function reads in chunks and manually splits on newlines, allowing
    arbitrarily large lines to be processed.

    Args:
        stream: The asyncio StreamReader to read from.

    Yields:
        Complete lines as bytes (without trailing newline).
    """
    buffer = b""
    warning_logged = False

    while True:
        # Read chunks without line-length restrictions
        try:
            chunk = await stream.read(_READ_CHUNK_SIZE)
        except Exception as e:
            logger.warning(f"Error reading stream chunk: {e}")
            break

        if not chunk:
            # End of stream - yield any remaining buffer content
            if buffer:
                yield buffer
            break

        buffer += chunk

        # Warn if buffer grows very large without seeing a newline
        # This could indicate malformed output or an unexpected data format
        if not warning_logged and len(buffer) > _BUFFER_WARNING_THRESHOLD:
            logger.warning(
                f"Stream buffer exceeded {_BUFFER_WARNING_THRESHOLD // (1024 * 1024)}MB "
                f"without newline (current size: {len(buffer) // (1024 * 1024)}MB). "
                "This may indicate malformed output from Claude Code."
            )
            warning_logged = True

        # Extract complete lines from buffer
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            warning_logged = False  # Reset warning after successful line extraction
            yield line


async def run_agent_async(
    prompt: str,
    *,
    config: ClaudeConfig | None = None,
    timeout: int | None = None,
    cwd: Path | str | None = None,
    on_output: Callable[[str], None] | None = None,
    model: str | None = None,
) -> AgentResult:
    """Run agent via Claude Code CLI in headless mode.

    Args:
        prompt: The prompt to send to Claude
        config: Agent configuration (uses defaults if None)
        timeout: Override config timeout (seconds)
        cwd: Working directory for the agent
        on_output: Optional callback for streaming output line-by-line
        model: Model to use (default: opus). Can be an alias ('opus', 'sonnet')
               or full model ID ('claude-opus-4-5-20251101')

    Returns:
        AgentResult with response and status. The result includes metadata
        about the actual model used via the 'model' key in metadata.

    Example:
        result = await run_agent_async(
            prompt="Fix the bug in main.py",
            cwd=Path.home() / "repos" / "my-repo",
        )
        if result.success:
            print("Agent completed successfully")
        else:
            print(f"Error: {result.error}")
    """
    config = config or ClaudeConfig()
    timeout = timeout or config.timeout
    cwd_path = Path(cwd) if cwd else config.cwd
    model = model or DEFAULT_MODEL

    # Build command
    # Note: --verbose is required when using --output-format stream-json
    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--verbose",
        "--model",
        model,
        "--output-format",
        "stream-json",
    ]

    logger.debug(f"Running: {' '.join(cmd)} (cwd={cwd_path})")

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    actual_model: str | None = None
    process = None

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd_path) if cwd_path else None,
        )

        # Send prompt via stdin and close
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()

        async with asyncio.timeout(timeout):
            # Read stdout using chunk-based reader to handle large JSON lines.
            # The default asyncio line iteration has a 64KB limit per line,
            # which can be exceeded by Claude's stream-json output containing
            # large thinking blocks or text content.
            async for line in _read_lines_unbuffered(process.stdout):
                line_text = line.decode().strip()
                if not line_text:
                    continue

                try:
                    event = json.loads(line_text)

                    # Extract model info
                    actual_model = _extract_model_from_event(event, actual_model)

                    # Extract text content
                    text = _extract_text_from_event(event)
                    if text:
                        stdout_parts.append(text)
                        if on_output:
                            on_output(text)

                except json.JSONDecodeError:
                    # Non-JSON line (shouldn't happen with stream-json)
                    logger.warning(f"Non-JSON line in output: {line_text[:100]}")

            # Read any remaining stderr
            stderr_data = await process.stderr.read()
            if stderr_data:
                stderr_parts.append(stderr_data.decode())

            # Wait for process to complete
            await process.wait()

        if actual_model:
            logger.info(f"Agent completed using model: {actual_model}")

        returncode = process.returncode if process.returncode is not None else -1
        if returncode == 0:
            return AgentResult(
                success=True,
                stdout="\n".join(stdout_parts),
                stderr="".join(stderr_parts),
                returncode=0,
                metadata={"model": actual_model} if actual_model else None,
            )
        else:
            stderr_text = "".join(stderr_parts)
            return AgentResult(
                success=False,
                stdout="\n".join(stdout_parts),
                stderr=stderr_text,
                returncode=returncode,
                error=_classify_error(returncode, stderr_text),
                metadata={"model": actual_model} if actual_model else None,
            )

    except TimeoutError:
        # Graceful shutdown: SIGTERM first, then SIGKILL
        if process:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()

        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr="".join(stderr_parts),
            returncode=-1,
            error=f"Timed out after {timeout} seconds",
            metadata={"model": actual_model} if actual_model else None,
        )

    except Exception as e:
        # Ensure process cleanup on any exception
        if process and process.returncode is None:
            process.kill()
            await process.wait()

        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr=str(e),
            returncode=-1,
            error=str(e),
            metadata={"model": actual_model} if actual_model else None,
        )

    finally:
        # Final cleanup to prevent zombies
        if process and process.returncode is None:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()


def run_agent(
    prompt: str,
    *,
    model: str | None = None,
    **kwargs: Any,
) -> AgentResult:
    """Synchronous wrapper for run_agent_async.

    See run_agent_async for full documentation.

    Args:
        prompt: The prompt to send to Claude
        model: Model to use (default: opus)
        **kwargs: Additional arguments passed to run_agent_async

    Example:
        result = run_agent("Explain the codebase", cwd="/path/to/repo")
        print(result.stdout)

        # Check what model was actually used:
        if result.metadata:
            print(f"Model used: {result.metadata.get('model')}")
    """
    return asyncio.run(run_agent_async(prompt, model=model, **kwargs))
