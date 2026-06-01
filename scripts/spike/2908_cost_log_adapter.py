"""Spike-only cost-log source adapter for the WS0 #2908 measurement.

This module is the **single source of truth** for parsing LiteLLM cost
records emitted by ``config/litellm/cost_callback.py``.  Both TASK-1-2's
per-event measurement code and slice-9 TASK-9-1's integration test
import from here so the in-cluster and CI/local code paths see the same
``{prompt_tokens, cache_read_tokens, cache_creation_tokens}`` payload
shape (R-2 mitigation).

What this adapter is NOT
------------------------
The operator's iteration-1 directive settled the cache-TTL question as
an INPUT: both Anthropic and Qwen routes' prefix caches survive >= 60
min idle with zero re-creation; observed BRC idles peak at ~10-13 min;
no keep-warm needed on either route.

This adapter is a pure parser + source-adapter.  It does no synthetic
waiting, no idle-duration bucketing, no cache-lifetime conclusion, and
no stop/go gating.  If TASK-1-2's per-event run happens to collect
cache_read vs cache_creation numbers as a side effect of cost
measurement, they are passed through verbatim -- framing them as
cache-lifetime evidence is forbidden by the spike report's ACs.

Two sources
-----------
The adapter handles both production paths:

  - ``read_kubectl_logs(deployment="egg-litellm")``: in-cluster --
    invokes ``kubectl logs deployment/<name>`` (capturing the LiteLLM
    pod's stdout, where ``_emit()`` in cost_callback.py writes).
  - ``read_tee_file(path)``: CI / local -- reads a file that captured
    LiteLLM stdout (typical pattern: ``litellm ... 2>&1 | tee log``).

Both return iterables of ``CostRecord`` dataclasses.

Field translation
-----------------
The LiteLLM emitter writes ``cached_tokens`` and ``cache_write_tokens``
(Anthropic SDK names).  The adapter exposes them under the canonical
``cache_read_tokens`` / ``cache_creation_tokens`` names that match the
issue body and the slice-1 report, so downstream code never has to
reach for two different field names.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 -- kubectl invocation is the documented path
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable, Iterator

__all__ = [
    "CostRecord",
    "parse_record",
    "iter_records",
    "read_tee_file",
    "read_kubectl_logs",
    "summarise",
]


@dataclass(frozen=True)
class CostRecord:
    """A single per-call cost line decoded from LiteLLM's stdout.

    Field names follow the canonical (issue body / spike report)
    convention, which differs from the upstream emit shape -- see
    :func:`parse_record` for the translation table.
    """

    timestamp: str
    session_id: str
    model: str | None
    cost: float | None
    prompt_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int

    def as_payload(self) -> dict[str, int | float | str | None]:
        """Canonical payload shape consumed by TASK-1-2 and TASK-9-1.

        The dict is intentionally narrow: just the three token counters
        plus the model name.  Anything richer (session totals, hit
        rate) is summarised via :func:`summarise`.
        """
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }


# Mapping LiteLLM emitter field name -> canonical adapter field name.
# See config/litellm/cost_callback.py:325-339 for the source shape.
_CALL_FIELD_MAP: dict[str, str] = {
    "cost": "cost",
    "prompt_tokens": "prompt_tokens",
    "cached_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_creation_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


def parse_record(line: str) -> CostRecord | None:
    """Decode one JSON line as written by ``cost_callback._emit()``.

    Returns ``None`` for lines that aren't cost records (e.g. plain
    LiteLLM info logs interleaved on the same stdout).

    The upstream emit shape (config/litellm/cost_callback.py:230-238):

        {"timestamp": "...", "severity": "INFO",
         "service": "litellm", "component": "cost_callback",
         "message": "litellm upstream cost + cache stats",
         "context": {"session_id": ..., "model": ...,
                     "call": {"cost": ..., "prompt_tokens": ...,
                              "cached_tokens": ...,
                              "cache_write_tokens": ...,
                              "reasoning_tokens": ...},
                     "session": {...},
                     "cache_hit_rate_pct": ...}}

    The translation table -- cached_tokens -> cache_read_tokens,
    cache_write_tokens -> cache_creation_tokens -- is the single
    place the upstream/canonical name divergence is resolved.
    """
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("component") != "cost_callback":
        return None
    context = payload.get("context") or {}
    if not isinstance(context, dict):
        return None
    call = context.get("call") or {}
    if not isinstance(call, dict):
        return None

    fields: dict[str, int | float | None] = {}
    for src, dst in _CALL_FIELD_MAP.items():
        raw = call.get(src)
        if raw is None and dst != "cost":
            fields[dst] = 0
        else:
            fields[dst] = raw

    def _int(name: str) -> int:
        raw = fields.get(name)
        return int(raw) if isinstance(raw, (int, float)) else 0

    cost_raw = fields.get("cost")
    cost = float(cost_raw) if isinstance(cost_raw, (int, float)) else None

    return CostRecord(
        timestamp=str(payload.get("timestamp", "")),
        session_id=str(context.get("session_id") or "_no_session"),
        model=context.get("model") if isinstance(context.get("model"), str) else None,
        cost=cost,
        prompt_tokens=_int("prompt_tokens"),
        cache_read_tokens=_int("cache_read_tokens"),
        cache_creation_tokens=_int("cache_creation_tokens"),
        reasoning_tokens=_int("reasoning_tokens"),
    )


def iter_records(source: Iterable[str]) -> Iterator[CostRecord]:
    """Decode an iterable of stdout lines into ``CostRecord`` instances.

    Non-cost lines are silently dropped -- LiteLLM mixes its own info
    logs onto the same stdout, and we don't want to fail the spike
    measurement on an unrelated INFO line.
    """
    for line in source:
        rec = parse_record(line)
        if rec is not None:
            yield rec


def read_tee_file(path: str | Path) -> Iterator[CostRecord]:
    """CI / local source: read records from a stdout-tee log file.

    Typical pattern that produces this file::

        litellm ... 2>&1 | tee /tmp/litellm-stdout.log

    The adapter reads the file line-by-line so it can stream over
    arbitrarily-large logs.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        yield from iter_records(fh)


def read_kubectl_logs(
    deployment: str = "egg-litellm",
    *,
    namespace: str | None = None,
    since: str | None = None,
    kubectl_bin: str | None = None,
) -> Iterator[CostRecord]:
    """In-cluster source: stream records from ``kubectl logs deployment/<name>``.

    Spawns ``kubectl`` and reads its stdout line-by-line.  Raises
    ``FileNotFoundError`` if the kubectl binary cannot be located --
    the spike report's slice-1 measurement section explicitly notes
    this as a precondition.
    """
    binary = kubectl_bin or shutil.which("kubectl")
    if binary is None:
        raise FileNotFoundError(
            "kubectl not found on PATH; install kubectl or pass kubectl_bin="
        )
    args = [binary, "logs", f"deployment/{deployment}"]
    if namespace is not None:
        args.extend(["-n", namespace])
    if since is not None:
        args.extend(["--since", since])
    # bandit S603/S607: we control all argv elements; deployment + namespace
    # come from caller, never shell-interpolated.
    proc = subprocess.Popen(  # noqa: S603
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None  # nosec B101 -- always set with PIPE
    try:
        yield from iter_records(_iter_stream(proc.stdout))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _iter_stream(stream: IO[str]) -> Iterator[str]:
    for line in stream:
        yield line


def summarise(records: Iterable[CostRecord]) -> dict[str, int | float | None]:
    """Aggregate per-event records into the spike-report headline shape.

    Output:
        {
          "calls": int,
          "prompt_tokens": int,
          "cache_read_tokens": int,
          "cache_creation_tokens": int,
          "cost_known_calls": int,
          "total_cost": float | None,
        }

    The spike report's "per-event cold-read cost in tokens" line is
    built from this summary divided by ``calls``; the adapter does the
    summing once so callers don't reimplement the loop.
    """
    calls = 0
    prompt = 0
    cache_read = 0
    cache_creation = 0
    cost_known_calls = 0
    cost_total = 0.0
    for rec in records:
        calls += 1
        prompt += rec.prompt_tokens
        cache_read += rec.cache_read_tokens
        cache_creation += rec.cache_creation_tokens
        if rec.cost is not None:
            cost_known_calls += 1
            cost_total += rec.cost
    return {
        "calls": calls,
        "prompt_tokens": prompt,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "cost_known_calls": cost_known_calls,
        "total_cost": cost_total if cost_known_calls > 0 else None,
    }


def _main(argv: list[str] | None = None) -> int:
    """Tiny CLI so the spike run log can capture adapter output directly.

    ``python3 scripts/spike/2908_cost_log_adapter.py --tee /tmp/foo.log``
    streams records as one-line JSON to stdout, with a final summary
    line tagged ``"summary": true``.

    The CLI is convenience only; production callers (TASK-1-2,
    TASK-9-1) import the functions above directly.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="2908_cost_log_adapter",
        description=(
            "Parse LiteLLM cost_callback stdout into "
            "{prompt_tokens, cache_read_tokens, cache_creation_tokens}."
        ),
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--tee", help="Path to a stdout-tee log file")
    src.add_argument("--kubectl", help="Deployment name to read via kubectl logs")
    parser.add_argument("--namespace", help="Kubernetes namespace (kubectl source only)")
    parser.add_argument("--since", help="kubectl --since value, e.g. 1h")
    args = parser.parse_args(argv)

    records: Iterator[CostRecord]
    if args.tee:
        records = read_tee_file(args.tee)
    else:
        records = read_kubectl_logs(
            args.kubectl, namespace=args.namespace, since=args.since
        )

    materialised: list[CostRecord] = []
    for rec in records:
        materialised.append(rec)
        print(json.dumps(rec.as_payload()))
    summary = summarise(materialised)
    summary_line = {"summary": True, **summary}
    print(json.dumps(summary_line))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
