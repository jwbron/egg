"""Anchor-based compaction integration for the harness.

Produces a callback suitable for :meth:`EventBus.on_compaction` that
updates the agent's anchor file whenever context compaction occurs,
keeping the anchor's progress and status fields in sync with the
agent's running state.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_compaction_callback(
    agent_id: str | None = None,
    base_dir: str | None = None,
) -> Callable[[str, int, int], None]:
    """Create a compaction event callback that updates the agent anchor.

    The returned callable has the signature
    ``(summary: str, tokens_before: int, tokens_after: int) -> None``
    and is intended to be registered via
    :meth:`EventBus.on_compaction`.

    On each invocation the callback:

    1. Loads the existing anchor for *agent_id* (via
       :func:`egg_anchor.loader.load_anchor`).
    2. Appends a progress item recording the compaction event.
    3. Saves the updated anchor back to disk.

    If *agent_id* is not provided, the ``AGENT_ANCHOR_ID`` environment
    variable is consulted.  When no agent ID can be determined, a no-op
    callback is returned.

    Import errors (e.g. ``egg_anchor`` not installed) are handled
    gracefully -- a no-op callback is returned.

    Args:
        agent_id: The agent's anchor identifier.
        base_dir: Optional base directory for anchor file storage.

    Returns:
        A callback suitable for :meth:`EventBus.on_compaction`.
    """
    resolved_id = agent_id or os.environ.get("AGENT_ANCHOR_ID", "").strip()
    if not resolved_id:
        return _noop_callback

    # Attempt to import egg_anchor up front so we can fall back to no-op
    # immediately if it is unavailable.
    try:
        from egg_anchor.loader import load_anchor, save_anchor
        from egg_anchor.models import AnchorStatus, ProgressItem, ProgressState
    except ImportError:
        logger.debug("egg_anchor not available; compaction callback is no-op")
        return _noop_callback

    def _on_compaction(summary: str, tokens_before: int, tokens_after: int) -> None:
        """Update the agent anchor with compaction information."""
        try:
            anchor = load_anchor(resolved_id, base_dir=base_dir)
            if anchor is None:
                logger.debug(
                    "No anchor found for agent %s; skipping compaction update",
                    resolved_id,
                )
                return

            # Append a progress item reflecting the compaction event.
            progress_item = ProgressItem(
                step="context_compaction",
                state=ProgressState.COMPLETE,
                detail=(f"Compacted context: {tokens_before} -> {tokens_after} tokens"),
                timestamp=datetime.now(UTC),
            )

            # Trim older compaction progress items if the list is getting long
            # to stay within anchor size limits.  Keep the most recent items.
            max_progress = 20
            new_progress = list(anchor.progress)
            new_progress.append(progress_item)
            if len(new_progress) > max_progress:
                new_progress = new_progress[-max_progress:]

            # Build updated anchor with new progress and updated timestamp.
            updated = anchor.model_copy(
                update={
                    "progress": new_progress,
                    "status": AnchorStatus.WORKING,
                    "meta": anchor.meta.model_copy(
                        update={
                            "updated_at": datetime.now(UTC),
                            "sequence": anchor.meta.sequence + 1,
                        }
                    ),
                }
            )

            save_anchor(updated, base_dir=base_dir)
            logger.debug(
                "Updated anchor %s after compaction (%d -> %d tokens)",
                resolved_id,
                tokens_before,
                tokens_after,
            )

        except Exception:
            # Never let anchor update failures disrupt the agent loop.
            logger.exception("Failed to update anchor %s after compaction", resolved_id)

    return _on_compaction


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _noop_callback(summary: str, tokens_before: int, tokens_after: int) -> None:
    """No-op compaction callback used when anchors are unavailable."""
