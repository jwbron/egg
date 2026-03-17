"""Atomic file I/O and API sync for agent anchors."""

import json
import logging
import os
import tempfile
from pathlib import Path

import requests

from .models import AgentAnchor

logger = logging.getLogger(__name__)

# Default anchor directory relative to repo root
ANCHOR_DIR = ".egg-state/agent-anchors"


def _anchor_path(agent_id: str, base_dir: str | None = None) -> Path:
    """Get the file path for an agent's anchor file."""
    if base_dir:
        return Path(base_dir) / ANCHOR_DIR / f"{agent_id}.json"
    repo_path = os.environ.get("EGG_REPO_PATH", ".")
    return Path(repo_path) / ANCHOR_DIR / f"{agent_id}.json"


def load_anchor(agent_id: str, base_dir: str | None = None) -> AgentAnchor | None:
    """Load an anchor from the local file system.

    Returns None if the file doesn't exist.
    """
    path = _anchor_path(agent_id, base_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return AgentAnchor.from_dict(data)
    except (json.JSONDecodeError, Exception) as e:
        logger.error("Failed to load anchor %s: %s", agent_id, e)
        return None


def save_anchor(anchor: AgentAnchor, base_dir: str | None = None) -> Path:
    """Save an anchor atomically using temp-file-then-rename.

    Returns the path where the anchor was saved.
    """
    path = _anchor_path(anchor.agent_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = json.dumps(anchor.to_dict(), indent=2)

    # Atomic write: write to temp file in same directory, then rename
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{anchor.agent_id}.",
            suffix=".tmp",
        )
        os.write(fd, data.encode())
        os.fsync(fd)
        os.close(fd)
        fd = None  # Mark as closed
        os.rename(tmp_path, str(path))
        tmp_path = None  # Mark as renamed (no cleanup needed)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    logger.debug("Saved anchor %s to %s", anchor.agent_id, path)
    return path


def sync_anchor_to_api(
    anchor: AgentAnchor,
    orchestrator_url: str | None = None,
) -> bool:
    """Sync an anchor to the orchestrator API (Redis backing store).

    Returns True if sync succeeded, False otherwise.
    """
    url = orchestrator_url or os.environ.get(
        "EGG_ORCHESTRATOR_URL", "http://egg-orchestrator:9849"
    )
    endpoint = f"{url}/api/v1/anchors/{anchor.agent_id}"

    try:
        resp = requests.post(
            endpoint,
            json=anchor.to_dict(),
            timeout=5,
        )
        if resp.status_code in (200, 201):
            logger.debug("Synced anchor %s to API", anchor.agent_id)
            return True
        else:
            logger.warning(
                "Failed to sync anchor %s: HTTP %d", anchor.agent_id, resp.status_code
            )
            return False
    except requests.RequestException as e:
        logger.warning("Failed to sync anchor %s: %s", anchor.agent_id, e)
        return False
