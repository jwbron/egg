"""Anchor-based compaction integration.

On compaction, persists state to the agent's anchor file
(.egg-state/agent-anchors/<agent-id>.json) before clearing context.
Post-compaction recovery reads the anchor.

See #1032 for the full anchor schema and recovery protocol.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


async def persist_compaction_to_anchor(
    summary: str,
    compaction_count: int,
    turn_number: int,
) -> bool:
    """Persist compaction summary to the agent's anchor file.

    Uses egg-orch anchor update if available.

    Returns True if persisted successfully.
    """
    agent_id = os.environ.get("AGENT_ANCHOR_ID", "")
    if not agent_id:
        logger.debug("No AGENT_ANCHOR_ID set, skipping anchor persistence")
        return False

    try:
        import asyncio

        progress_data = json.dumps(
            {
                "compaction_count": compaction_count,
                "last_compaction_turn": turn_number,
                "summary_length": len(summary),
            }
        )

        cmd = [
            "egg-orch",
            "anchor",
            "update",
            "--progress",
            progress_data,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

        if proc.returncode != 0:
            logger.warning(f"Anchor update failed: {stderr.decode()}")
            return False

        return True

    except Exception as e:
        logger.warning(f"Failed to persist compaction to anchor: {e}")
        return False
