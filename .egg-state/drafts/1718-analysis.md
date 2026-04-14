## Analysis — Issue #1718

### Problem
During BRC consensus, agents have no clear structured path to coordinate with peers. In the issue-1707 pipeline, the coder embedded a directed request ("tester agent should push those") in its broadcast proposal body. The tester eventually wrote tests independently ~10 minutes later. The indirection caused delay and left no structured record.

### System context
In concurrent mode, agents run the BRC protocol. The `MessageType` enum (`shared/egg_orchestrator/types.py:56`) already defines `PROGRESS`, `QUESTION`, `STATUS`, `AGENT_FAILED`, `HANDOFF`, plus `CONSENSUS_*`. The message store supports `to_role` targeting. Agents receive their BRC instructions via `_build_brc_preamble` (`orchestrator/routes/pipelines.py:5251`), which today covers propose/ACK/NACK/confirm/stay-alive but says nothing about directed peer-to-peer coordination.

### Root cause / real gap

The issue's proposal #1 is **already implemented**:
- `egg-orch message send --to <role> --type <type> --subject <s> --body <b>` exists at `sandbox/egg_lib/orch_cli.py:978`, registered at line 1777.
- All `MessageType` values including `HANDOFF` are accepted by the server endpoint `POST /api/v1/pipelines/{id}/messages`.
- Minor nit: the CLI `--type` help text lists "(PROGRESS, QUESTION, STATUS)" and omits `HANDOFF`, making it discoverable only by reading the docs table in `concurrent-execution.md:144-149`.

The actual gap is prompt + documentation:
- `_build_brc_preamble` never tells agents to use `egg-orch message send` for directed coordination. Agents are told to poll messages, propose, ACK/NACK, re-review — but not when/how to initiate directed peer traffic.
- `docs/guides/concurrent-execution.md` lists message types under an HTTP API section, but has no CLI-oriented guidance on *when to use* directed messaging vs. embedding in proposal text, and no worked example.

### Files affected
- `orchestrator/routes/pipelines.py` — extend `_build_brc_preamble` (~5251) with a role-gated "Directed Coordination" section for producers and reviewers.
- `sandbox/egg_lib/orch_cli.py:1782` — update `--type` help text to include `HANDOFF`.
- `docs/guides/concurrent-execution.md` — add a "Directed Coordination" subsection under "Sending Messages" with CLI form, when-to-use rules, and the coder→tester worked example from issue-1707.
- `orchestrator/tests/test_pipeline_prompts.py` — add a test asserting the BRC preamble contains the new guidance.

### Risks / edge cases
- Prompt-and-docs scope only; no runtime behavior changes. Agents that ignore the new guidance fail open to today's behavior.
- Without #1717 landing, directed HANDOFF/QUESTION/STATUS messages still won't be persisted to PR history — so value is partially realized until #1717 lands. Issue flags this; landing #1718 alone is still a net positive.
- Prompt bloat: the preamble is already long. New section should be tight (~8–12 lines per role) and role-gated.