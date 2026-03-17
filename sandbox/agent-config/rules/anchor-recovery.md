# Post-Compaction Recovery via Agent Anchor

When your context window has been cleared and you are restarting, follow this
protocol to recover your working state from the agent anchor file.

## Recovery Steps

1. **Read your anchor**: Run `egg-orch anchor show` to load your persisted state.
   This returns your task progress, decisions, BRC consensus state, key context,
   errors encountered, and files modified.

2. **Catch up on messages**: Use the `last_message_id` from your anchor's
   `brc_state` to poll only new messages:
   ```bash
   egg-orch message poll --since <last_message_id> --wait 5
   ```

3. **Verify file state**: Cross-check `files_modified` from your anchor against
   `git log --oneline -10` and `git diff --stat` to confirm your working tree
   matches the anchor's expectations.

4. **Resume from current progress**: Look at the `progress` array in your anchor.
   Find the last item with `state: "working"` or the first with `state: "pending"`.
   That is your current task.

5. **Update your anchor**: After recovery, update your anchor to reflect the
   resumed state:
   ```bash
   egg-orch anchor update --status working
   ```

## Important Notes

- The anchor is your **single source of truth** after context clear.
- Always update your anchor at natural milestones (task completion, decisions,
  errors) so recovery is accurate.
- If the anchor file is missing or corrupted, fall back to reading the contract
  (`egg-contract show`) and message bus to reconstruct state.
- Your anchor is scoped to your agent ID — you cannot read or write other
  agents' anchors directly. Use `egg-orch anchor show --team` for the team view.
