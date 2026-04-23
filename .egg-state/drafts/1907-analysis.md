## Task Analysis for #1907

**Problem statement**: When Anthropic's upstream API resets an SSE TCP connection mid-stream, the gateway's `/v1/messages` proxy lets the `httpx.ReadError` propagate out of its streaming generator. The downstream SDK sees a truncated SSE with no terminating event and reports a fatal `socket connection was closed unexpectedly`. The agent exits, the Job hits `BackoffLimitExceeded`, and all in-flight work (32 turns / $1.33 / 4.5 min on the observed incident) is lost — while the gateway itself is healthy.

**Source context**: Issue #1907 reports a specific incident on 2026-04-22 in pipeline `issue-1901` (plan phase, architect role). Gateway pod was up 35min with 0 restarts — only one upstream TCP connection died. The issue ranks four remediations and recommends (A) short-term and (B) medium-term. Distinct from #1883 (gateway pod restart) and #1873 (turn-1 transient retry). Related to #1887 (SSE parsing groundwork already merged in `8d81d7b6b`).

**Workarounds**: None — agents fail permanently on upstream RST. Only mitigation today is the full-pipeline restart cost.

**System context**: `proxy_anthropic_messages` in `gateway/gateway.py:5202` is the `/v1/messages` endpoint. For streaming requests it calls `client.send(http_request, stream=True)` to open an upstream SSE connection (line 5251), then returns a Flask `Response` wrapping a `generate()` generator (5270–5308) that iterates `upstream.iter_bytes()` (5273), feeds chunks to an `_SSEAccumulator` for transcript capture, and yields each chunk downstream. The shared `httpx.Client` is a singleton with a keepalive pool (`max_keepalive_connections=20`, line 4774). The try/except block at 5333–5364 catches `ConnectError`, `TimeoutException`, and generic `Exception`, but only protects the synchronous `client.send()` and non-streaming path — exceptions raised **inside `generate()` during streaming** have already fired after `Response(…)` returned, so they bypass this handler and bubble up to waitress as `ERROR:waitress:Exception while serving /v1/messages`.

**Technical root cause**: Two gaps in the streaming path:

1. **No retry on early upstream failure.** `client.send(http_request, stream=True)` at line 5251 can raise `httpx.ReadError` if the pooled connection is stale or the upstream resets before returning response bytes. Today this falls through to the generic 502 handler — no retry. A fresh upstream connection would almost always succeed.
2. **No error envelope on mid-stream reset.** The `for chunk in upstream.iter_bytes():` loop at line 5273 has no `except httpx.ReadError`. When upstream RSTs after headers + some bytes have flowed (the observed incident), the `ReadError` propagates out of the generator mid-response. Flask/waitress terminates the transport without writing any terminating SSE event. The SDK downstream sees a truncated stream and reports a fatal connection-closed error, and hits `RuntimeError: aclose(): asynchronous generator is already running` as a side-effect of the abnormal close.

**Files affected**:
- `gateway/gateway.py` — `proxy_anthropic_messages` (~line 5202): wrap `client.send(...)` + first-chunk read in a bounded retry loop ((A)); wrap `generate()`'s `iter_bytes` loop with `httpx.ReadError` handling that emits a synthetic SSE `error` event and closes cleanly ((B)).
- `gateway/tests/test_proxy_anthropic_stream.py` (new) — add tests covering: early upstream reset retried successfully; persistent early reset falls back to 502; mid-stream reset emits a terminating SSE `error` event and downstream receives a clean close.

**Risks / edge cases**:
- **Retry idempotency**: only retry if *no bytes have been yielded downstream yet* — once the agent has received partial output, re-issuing the request could produce duplicate work. The retry window must end at the first yielded chunk.
- **Bounded retries**: cap at 1 retry with short backoff to avoid amplifying load during a real outage.
- **Transcript accumulator**: on mid-stream error, the `_SSEAccumulator` may have partial data — `accumulator.result()` must still be flushed in `finally` (already done) and handle a partial parse without raising. Verify current behavior.
- **Synthetic error event format**: must match Anthropic's `event: error` convention so the SDK routes it through its normal error path, not the connection-closed fatal path.
- **No interaction with `/v1/messages/count_tokens`**: that path is non-streaming and already guarded — no change needed.
- **No interaction with session/auth**: the retry uses the same `headers` and `request_body`; credential injection happens once before the loop.