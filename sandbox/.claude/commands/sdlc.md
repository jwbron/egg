# SDLC Pipeline

This command has been replaced by the `egg-sdlc` CLI tool.

## Usage

Run `egg-sdlc` directly from the terminal:

```bash
# Issue mode: start pipeline for a GitHub issue
egg-sdlc <issue_number>
egg-sdlc <issue_number> --repo <owner/repo>

# Local mode: prompt-driven pipeline (no GitHub)
egg-sdlc
```

The `egg-sdlc` CLI provides:
- Real-time DAG visualization of pipeline progress
- Interactive HITL checkpoints with editor, Claude, and feedback options
- Direct pipeline control (approve, provide feedback, cancel)

Do NOT use `curl` commands to interact with the orchestrator API manually.
Use `egg-sdlc` instead — it handles the full pipeline lifecycle.

## Related Commands

- `egg-pipeline-watch <pipeline_id>`: Watch-only mode (no HITL interaction)
- `egg-contract show`: View contract state
