"""Session forwarding for egg-launcher.

This module handles stdin/stdout forwarding between the launcher and
sandbox container for interactive sessions.
"""

import os
import pty
import select
import subprocess
import sys
import tty


class SessionForwarder:
    """Handles interactive session forwarding to sandbox container.

    This class manages stdin/stdout forwarding using a pseudo-terminal (pty)
    to provide a fully interactive experience when running egg in a container.
    """

    def __init__(self, container_name: str):
        """Initialize the session forwarder.

        Args:
            container_name: Name of the Docker container to attach to
        """
        self.container_name = container_name

    def attach(self) -> int:
        """Attach to the container's stdin/stdout.

        This creates a pty and forwards all input/output between the
        terminal and the container.

        Returns:
            Exit code from the container
        """
        # Use docker exec to attach to the container
        cmd = ["docker", "exec", "-it", self.container_name, "/bin/bash"]

        try:
            result = subprocess.run(cmd)
            return result.returncode
        except KeyboardInterrupt:
            return 130  # Standard exit code for Ctrl+C

    def run_command(self, command: list[str], interactive: bool = True) -> int:
        """Run a command in the container.

        Args:
            command: Command and arguments to run
            interactive: Whether to run interactively

        Returns:
            Exit code from the command
        """
        cmd = ["docker", "exec"]

        if interactive:
            cmd.extend(["-it"])

        cmd.append(self.container_name)
        cmd.extend(command)

        try:
            result = subprocess.run(cmd)
            return result.returncode
        except KeyboardInterrupt:
            return 130


class PtyForwarder:
    """Low-level PTY forwarding for full terminal control.

    This class provides a more sophisticated terminal forwarding mechanism
    using Python's pty module for cases where docker exec -it isn't sufficient.
    """

    def __init__(self):
        """Initialize the PTY forwarder."""
        self._old_tty_settings = None

    def forward(self, command: list[str]) -> int:
        """Forward terminal I/O to a subprocess.

        Args:
            command: Command to execute

        Returns:
            Exit code from the subprocess
        """
        # Save original tty settings
        if sys.stdin.isatty():
            self._old_tty_settings = tty.tcgetattr(sys.stdin)

        try:
            # Create a pseudo-terminal
            master_fd, slave_fd = pty.openpty()

            # Start the subprocess
            process = subprocess.Popen(
                command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )

            os.close(slave_fd)

            # Put terminal in raw mode
            if sys.stdin.isatty():
                tty.setraw(sys.stdin)

            # Forward I/O
            try:
                self._forward_io(master_fd)
            except Exception:
                pass

            # Wait for process to finish
            process.wait()
            return process.returncode

        finally:
            # Restore tty settings
            if self._old_tty_settings:
                tty.tcsetattr(sys.stdin, tty.TCSADRAIN, self._old_tty_settings)

    def _forward_io(self, master_fd: int) -> None:
        """Forward I/O between stdin/stdout and master pty.

        Args:
            master_fd: File descriptor for master end of pty
        """
        while True:
            rlist, _, _ = select.select([sys.stdin, master_fd], [], [], 0.1)

            if sys.stdin in rlist:
                data = os.read(sys.stdin.fileno(), 1024)
                if not data:
                    break
                os.write(master_fd, data)

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 1024)
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                except OSError:
                    break


def run_interactive_sandbox(
    sandbox_image: str,
    network: str,
    env: dict[str, str],
    volumes: list[str],
    container_name: str | None = None,
) -> int:
    """Run a sandbox container interactively.

    This function handles all the complexity of running an interactive
    Docker container with proper terminal forwarding.

    Args:
        sandbox_image: Docker image to run
        network: Docker network to attach to
        env: Environment variables
        volumes: Volume mount specifications
        container_name: Optional container name

    Returns:
        Exit code from the container
    """
    cmd = ["docker", "run", "--rm", "-it"]

    if container_name:
        cmd.extend(["--name", container_name])

    cmd.extend(["--network", network])

    for key, value in env.items():
        cmd.extend(["-e", f"{key}={value}"])

    for volume in volumes:
        cmd.extend(["-v", volume])

    cmd.append(sandbox_image)

    try:
        result = subprocess.run(cmd)
        return result.returncode
    except KeyboardInterrupt:
        return 130


def run_print_mode_sandbox(
    sandbox_image: str,
    network: str,
    env: dict[str, str],
    volumes: list[str],
    prompt: str,
    container_name: str | None = None,
    timeout: int | None = None,
) -> int:
    """Run a sandbox container in print mode.

    This function runs the sandbox non-interactively with a prompt,
    suitable for CI/CD or batch processing.

    Args:
        sandbox_image: Docker image to run
        network: Docker network to attach to
        env: Environment variables
        volumes: Volume mount specifications
        prompt: The prompt to execute
        container_name: Optional container name
        timeout: Optional timeout in seconds

    Returns:
        Exit code from the container
    """
    cmd = ["docker", "run", "--rm"]

    if container_name:
        cmd.extend(["--name", container_name])

    cmd.extend(["--network", network])

    for key, value in env.items():
        cmd.extend(["-e", f"{key}={value}"])

    for volume in volumes:
        cmd.extend(["-v", volume])

    cmd.append(sandbox_image)

    # Add claude command
    cmd.extend(
        [
            "claude",
            "--dangerously-skip-permissions",
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            prompt,
        ]
    )

    try:
        result = subprocess.run(cmd, timeout=timeout)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"Sandbox execution timed out after {timeout}s", file=sys.stderr)
        return 124  # Standard timeout exit code
    except KeyboardInterrupt:
        return 130
