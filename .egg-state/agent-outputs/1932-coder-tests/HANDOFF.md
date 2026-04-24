# Coder-authored test handoff — issue #1932

The coder role produced these test files alongside the Phase 1 + 2
implementation to self-validate the work.  Per the gateway file
boundaries the coder role cannot push `orchestrator/tests/**`, so
the fully-passing test files are staged here for the tester to
drop in verbatim (or adapt).

All cases pass against commit `1258ff399`:

| File                                              | Target path                                                    | Pass count |
| ------------------------------------------------- | -------------------------------------------------------------- | ---------- |
| `test_pipelines_status_wait_route.py`             | `orchestrator/tests/test_pipelines_status_wait_route.py`       | 16/16      |
| `test_events_event_sequence.py`                   | `orchestrator/tests/test_events_event_sequence.py`             | 7/7        |
| `test_mcp_tools_additions.py`                     | `orchestrator/tests/test_mcp_tools.py` (append classes)        | 8/8        |

## Summary of coverage

- **Route** (16 cases) — cursor parse/build, timeout envelope,
  EventBus wake, DECISION_RESOLVED exclusion, since-cursor skip,
  OVERSEER_ALERT message wake, 400 malformed cursor, 404 unknown
  pipeline, 400 invalid wait, wait clamp, `egg_inflight_host_waits`
  lifecycle, queue-full burst.
- **Event sequence** (7 cases) — default 0, `to_dict` includes
  sequence, monotonic publish, caller-supplied sequence overwritten,
  100 concurrent publishes across 8 threads (1..100, no gaps),
  `current_sequence()` tracks tip, existing consumers still work.
- **MCP tool** (8 cases) — dispatcher wiring, `no_change`
  passthrough, `changed=true` snapshot merge (event + message),
  `since` URL-encoded, empty `since` omitted, `_build_status_snapshot`
  == `_handle_get_status`, R16 double-sleep regression.

## Test execution

From `orchestrator/`:

```bash
pytest tests/test_pipelines_status_wait_route.py -v
pytest tests/test_events_event_sequence.py -v
pytest tests/test_mcp_tools.py -v
```

Existing `test_mcp_tools.py` suite (163 cases) still passes after
the `_build_status_snapshot` extraction — the refactor is pure
behaviour-preserving.
