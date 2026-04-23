# Plan: Make gateway `/v1/messages` proxy resilient to upstream TCP resets

## Summary

Make the gateway's Anthropic `/v1/messages` proxy resilient to two classes of upstream TCP resets: (A) pre-stream resets (connection-pool stale conn, very-early RST) are handled by a bounded transparent retry — downstream SDK never sees an error; (B) mid-stream resets are caught inside the `iter_bytes()` loop and surfaced as a well-formed synthetic SSE `event: error` frame, letting the SDK fail gracefully instead of dying on a truncated socket. Both fixes live entirely inside `proxy_anthropic_messages()` in `gateway/gateway.py`. The issue explicitly recommends both as complementary — (A) catches the cheap case transparently, (B) makes the unavoidable mid-stream case recoverable at the agent layer. Neither attempts full stream resumption (Anthropic has no resume tokens; partial generation is lost on mid-stream reset regardless).

**Risks / edge cases**:
- Pre-stream retry must be capped at one attempt and conditional on zero downstream bytes having flowed — retrying after any chunk has been yielded is unsafe (Anthropic treats each POST independently, would double-charge and produce two divergent generations mixed on the downstream wire).
- Must catch both `httpx.ReadError` and `httpx.RemoteProtocolError` — ECONNRESET surfaces as either depending on where in httpcore's state machine the RST arrives.
- Synthetic SSE frame must conform to Anthropic's error-event format (`event: error\ndata: {"type": "error", "error": {...}}\n\n`); `_SSEAccumulator._process_event` at `gateway/gateway.py:5114` already handles that payload shape, so the synthetic event round-trips correctly through the existing accumulator for transcript capture.
- The existing `finally: upstream.close()` + `_capture_streaming_response` path at `gateway/gateway.py:5286–5301` must still execute on mid-stream error so operators retain a transcript of the failed generation.
- Existing top-level `except httpx.ConnectError / TimeoutException / Exception` at `gateway/gateway.py:5333–5364` keeps its current behavior for non-reset errors. Only `ReadError` and `RemoteProtocolError` paths change.

## Implementation

### Phase 1: Implement

Add gateway-side resilience for upstream TCP resets on `/v1/messages` streaming requests.

**Tasks**:
1. **[task-1-1]** In `gateway/gateway.py`, inside `proxy_anthropic_messages()` streaming branch (lines ~5241–5308), add a bounded pre-stream retry around `client.send(http_request, stream=True)` plus the first-chunk prime. On `httpx.ReadError` or `httpx.RemoteProtocolError` raised before any downstream byte has been yielded, close the failed upstream, rebuild the request, and retry once. If the second attempt succeeds, proceed normally. If it fails too, fall through to the existing error-return path. Acceptance: verified by test 1-3(a) and 1-3(b).
2. **[task-1-2]** In the same function's `generate()` closure (lines ~5270–5301), wrap the `for chunk in upstream.iter_bytes()` loop with a try/except for `httpx.ReadError` and `httpx.RemoteProtocolError`. On catch, yield a synthetic SSE frame shaped as `event: error\ndata: {"type": "error", "error": {"type": "api_error", "message": "upstream connection reset"}}\n\n` then return cleanly. Feed the synthetic frame into the accumulator so transcript capture records it. Preserve the existing `finally: upstream.close()` + `_capture_streaming_response` behavior. Log the reset with `logger.warning` including `container_id` and `bytes_seen` so operators can correlate with incident analysis. Acceptance: verified by test 1-3(c).
3. **[task-1-3]** In `tests/gateway/test_anthropic_proxy.py`, extend `TestStreamingResponse` with three new tests: (a) `client.send()` raises `httpx.ReadError` once, second attempt returns a valid iterator — assert downstream sees a clean 200 SSE response and `send` was called twice; (b) `iter_bytes()` yields nothing and raises `httpx.ReadError` on first iteration → expect the retry path to re-prime and produce a normal stream; (c) `iter_bytes()` yields one chunk then raises `httpx.RemoteProtocolError` → expect the downstream body to contain the original chunk followed by a well-formed `event: error` SSE frame and the stream to close without exception. Use the existing `mock_response.iter_bytes = MagicMock(return_value=iter([...]))` pattern, wrapping the iterator in a helper that raises after N chunks. Acceptance: all three tests pass; existing streaming tests continue to pass.

```yaml
# yaml-tasks
pr:
  title: "Gateway: retry pre-stream ECONNRESET, synthesize SSE error on mid-stream reset"
  description: |
    Fixes #1907. Makes the gateway's `/v1/messages` proxy resilient to upstream Anthropic TCP resets.

    - (A) Pre-stream retry: if `client.send()` or the first `iter_bytes()` prime raises `httpx.ReadError`/`RemoteProtocolError` before any downstream byte has flowed, transparently re-issue the upstream request once. Downstream SDK never sees the error.
    - (B) Mid-stream synthetic error: if a reset arrives after bytes have already streamed, catch it inside `generate()`, yield a well-formed SSE `event: error` frame, and close the stream cleanly so the agent's SDK fails gracefully instead of dying on a truncated socket.

    Distinct from #1883 (gateway pod restart); this covers the gateway-healthy/upstream-unhealthy case where the fix belongs inside the gateway.
  test_plan: |
    - Automated: new tests in `tests/gateway/test_anthropic_proxy.py::TestStreamingResponse` — `send()` reset → retry success, first-chunk reset → retry success, mid-stream reset → synthetic error frame. Existing streaming tests continue to pass.
    - Manual: run `pytest tests/gateway/test_anthropic_proxy.py -v` and confirm all green.
  manual_steps: |
    Pre-merge: none beyond CI.
    Post-merge: observe gateway logs for `logger.warning("upstream reset", ...)` entries over the next 24h to confirm the code path is exercising under real traffic and not spuriously triggering on healthy streams.
phases:
  - id: 1
    name: Implement
    goal: "Gateway transparently retries pre-stream upstream resets and emits a clean SSE error event on mid-stream resets"
    tasks:
      - id: task-1-1
        description: "In gateway/gateway.py proxy_anthropic_messages() streaming branch, add bounded (1x) retry around client.send() and first-chunk prime on httpx.ReadError/RemoteProtocolError before any downstream byte has been yielded. Close the failed upstream, rebuild the request, retry once. On second failure, fall through to the existing error-return path."
        acceptance: "When client.send() raises ReadError once, the retry succeeds and downstream sees a clean 200 SSE response. When the first iter_bytes() call raises ReadError, the gateway re-primes and produces a normal stream. Both verified by new unit tests."
        files:
          - gateway/gateway.py
      - id: task-1-2
        description: "In the generate() closure inside proxy_anthropic_messages(), wrap the iter_bytes() for-loop with try/except for httpx.ReadError and httpx.RemoteProtocolError. On catch, yield a well-formed synthetic SSE frame (event: error with Anthropic-style payload), feed it through the accumulator, log a warning with container_id and bytes_seen, and return cleanly. Preserve the existing finally: upstream.close() and _capture_streaming_response behavior."
        acceptance: "When iter_bytes() raises after one chunk has been yielded, the downstream body contains the original chunk followed by a well-formed `event: error` SSE frame, the stream closes without raising, and _capture_streaming_response still runs. Verified by a new unit test."
        files:
          - gateway/gateway.py
      - id: task-1-3
        description: "In tests/gateway/test_anthropic_proxy.py, extend TestStreamingResponse with three tests covering: (a) client.send() raises ReadError once then succeeds on retry; (b) iter_bytes() raises on first iteration then succeeds on retry; (c) iter_bytes() raises RemoteProtocolError after one chunk and the downstream body ends with a synthetic event: error frame. Use a small helper to wrap an iterator so it raises after N yielded chunks."
        acceptance: "All three new tests pass. Existing tests in TestStreamingResponse (test_streaming_request_detected, test_streaming_content_type_forwarded) continue to pass."
        files:
          - tests/gateway/test_anthropic_proxy.py
```
