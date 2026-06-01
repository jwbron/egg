"""Unit tests for scripts/spike/2908_cost_log_adapter.py.

The cost-log source adapter parses LiteLLM cost-callback stdout into
the canonical ``{prompt_tokens, cache_read_tokens, cache_creation_tokens}``
payload shape (issue #2908 slice-1 TASK-1-3).

The acceptance criteria locks in three properties:

1. Both source paths (``kubectl logs deployment/egg-litellm`` and the
   stdout-tee file) return the **same** ``{prompt_tokens,
   cache_read_tokens, cache_creation_tokens}`` shape.
2. The adapter is importable from TASK-1-2's per-event measurement
   code AND from slice-9 TASK-9-1's integration test.
3. No multi-idle-duration injection, no "TTL ceiling" /
   "TTL survival" / "bracket" assertions, no synthetic idle — all
   forbidden by the operator's iteration-1 directive.

The module under test lives at ``scripts/spike/2908_cost_log_adapter.py``;
Python cannot ``import`` a filename starting with a digit directly, so
these tests load it via ``importlib.util.spec_from_file_location``. This
mirrors the existing pattern in tests/scripts/.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_ADAPTER_PATH = _REPO / "scripts" / "spike" / "2908_cost_log_adapter.py"


def _load_adapter():
    """Load ``2908_cost_log_adapter`` from disk.

    The leading digit in the filename rules out normal import, so we
    spec-load it under a sanitized module name. We register the loaded
    module in ``sys.modules`` so the dataclass identity stays stable
    across tests (without it, repeated ``_load_adapter()`` calls would
    create distinct ``CostRecord`` classes).
    """
    name = "egg_spike_2908_cost_log_adapter"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ADAPTER_PATH)
    assert spec and spec.loader, f"cannot load {_ADAPTER_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter = _load_adapter()


# ─── Fixture helpers ──────────────────────────────────────────────────────


def _emit_line(
    *,
    session_id: str = "sess-1",
    model: str | None = "openrouter/qwen/qwen3-max",
    cost: float | None = None,
    prompt_tokens: int = 1000,
    cached_tokens: int = 600,
    cache_write_tokens: int = 100,
    reasoning_tokens: int = 50,
    timestamp: str = "2026-06-01T02:50:00+00:00",
    component: str = "cost_callback",
    drop_call: bool = False,
) -> str:
    """Build a JSON line that mirrors ``cost_callback._emit()``'s shape.

    The cost_callback source-of-truth lives at config/litellm/cost_callback.py
    line 230-241 (top-level frame) and line 325-339 (the ``context.call``
    payload). Field names use the upstream emit convention
    (``cached_tokens`` / ``cache_write_tokens``) — the adapter's job is
    to translate to the canonical names.
    """
    context: dict[str, object] = {
        "session_id": session_id,
        "model": model,
    }
    if not drop_call:
        context["call"] = {
            "cost": cost,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
        }
    payload = {
        "timestamp": timestamp,
        "severity": "INFO",
        "service": "litellm",
        "component": component,
        "message": "litellm upstream cost + cache stats",
        "context": context,
    }
    return json.dumps(payload)


# ─── parse_record() ──────────────────────────────────────────────────────


class TestParseRecord:
    def test_canonical_field_translation(self):
        """``cached_tokens`` → ``cache_read_tokens`` and
        ``cache_write_tokens`` → ``cache_creation_tokens`` — the AC's
        single justification for the adapter existing."""
        rec = adapter.parse_record(_emit_line())
        assert rec is not None
        assert rec.prompt_tokens == 1000
        assert rec.cache_read_tokens == 600
        assert rec.cache_creation_tokens == 100
        assert rec.reasoning_tokens == 50

    def test_streaming_cost_is_none(self):
        """Streaming path drops upstream cost — adapter must preserve
        ``None`` (NOT coerce to 0.0) so callers can treat it as
        'unknown', mirroring the cost_callback policy."""
        rec = adapter.parse_record(_emit_line(cost=None))
        assert rec is not None
        assert rec.cost is None

    def test_nonstreaming_cost_is_float(self):
        rec = adapter.parse_record(_emit_line(cost=0.0123))
        assert rec is not None
        assert rec.cost == 0.0123
        assert isinstance(rec.cost, float)

    def test_skips_non_cost_callback_lines(self):
        """LiteLLM mixes its own INFO logs onto the same stdout. The
        adapter must drop everything that isn't tagged with
        ``component: cost_callback`` so the parser is safe to run over
        the full pod log."""
        for comp in ("litellm_proxy", "litellm_router", "", None):
            kwargs = {"component": comp} if comp is not None else {}
            line = _emit_line(**kwargs) if kwargs else _emit_line(
                component=""
            ).replace('"component": ""', '"component": null')
            assert adapter.parse_record(line) is None

    def test_skips_blank_and_non_json_lines(self):
        for line in ("", "   \n", "not a json line", "<INFO> startup", "{"):
            assert adapter.parse_record(line) is None

    def test_skips_malformed_json(self):
        """A line that *starts* with ``{`` but isn't valid JSON must
        not raise — the parser silently drops it. (Important: pod logs
        can have partial lines truncated mid-emit.)"""
        # No exception — silent drop.
        assert adapter.parse_record('{"timestamp": "x", "context"') is None

    def test_skips_non_object_root(self):
        """JSON arrays / scalars at the top level must drop, not
        crash."""
        assert adapter.parse_record('[1, 2, 3]') is None

    def test_handles_leading_whitespace(self):
        rec = adapter.parse_record("   " + _emit_line() + "\n")
        assert rec is not None
        assert rec.prompt_tokens == 1000

    def test_missing_call_yields_zero_counts(self):
        """A cost_callback line with the context but no ``call`` key —
        should not crash; should produce zeros (NOT raise) so a
        truncated emit doesn't poison the stream."""
        rec = adapter.parse_record(_emit_line(drop_call=True))
        assert rec is not None
        assert rec.prompt_tokens == 0
        assert rec.cache_read_tokens == 0
        assert rec.cache_creation_tokens == 0
        assert rec.reasoning_tokens == 0
        assert rec.cost is None

    def test_missing_context_returns_none(self):
        """``context`` is a string instead of dict — drop the line
        rather than guess."""
        payload = {
            "timestamp": "x",
            "component": "cost_callback",
            "context": "not-a-dict",
        }
        assert adapter.parse_record(json.dumps(payload)) is None

    def test_session_id_defaults_to_no_session(self):
        """The cost_callback uses ``_no_session`` when the request
        carries no session header. The adapter must mirror that
        fallback rather than emit an empty string."""
        line = _emit_line(session_id="")
        rec = adapter.parse_record(line)
        # Empty string is falsy -> falls back to _no_session.
        assert rec is not None
        assert rec.session_id == "_no_session"

    def test_model_can_be_none(self):
        line = _emit_line(model=None)
        rec = adapter.parse_record(line)
        assert rec is not None
        assert rec.model is None

    def test_non_string_model_returns_none(self):
        """If model isn't a string, drop it to None rather than crash
        on downstream usage."""
        # Hand-craft a payload with non-string model.
        payload = {
            "timestamp": "x",
            "component": "cost_callback",
            "context": {
                "session_id": "s",
                "model": 123,
                "call": {"prompt_tokens": 10, "cached_tokens": 5},
            },
        }
        rec = adapter.parse_record(json.dumps(payload))
        assert rec is not None
        assert rec.model is None

    def test_floats_are_int_coerced_on_token_fields(self):
        """LiteLLM occasionally emits floats for token counts (e.g.
        from aggregate math). The dataclass field types are int, so
        the adapter must coerce on the way in."""
        line = _emit_line(prompt_tokens=1000.0, cached_tokens=600.0)  # type: ignore[arg-type]
        rec = adapter.parse_record(line)
        assert rec is not None
        assert rec.prompt_tokens == 1000
        assert rec.cache_read_tokens == 600
        assert isinstance(rec.prompt_tokens, int)
        assert isinstance(rec.cache_read_tokens, int)


# ─── as_payload() ────────────────────────────────────────────────────────


class TestAsPayload:
    def test_canonical_payload_shape_matches_ac(self):
        """The AC names exactly three counter fields plus model — make
        sure the payload doesn't leak more (or fewer) keys, so
        downstream consumers (TASK-1-2 + slice-9 TASK-9-1) get the
        contract the AC promised."""
        rec = adapter.parse_record(_emit_line())
        assert rec is not None
        payload = rec.as_payload()
        assert set(payload.keys()) == {
            "model",
            "prompt_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        }
        assert payload["prompt_tokens"] == 1000
        assert payload["cache_read_tokens"] == 600
        assert payload["cache_creation_tokens"] == 100


# ─── iter_records() ───────────────────────────────────────────────────────


class TestIterRecords:
    def test_iterates_multiple_records(self):
        lines = [_emit_line(session_id=f"s{i}") for i in range(3)]
        records = list(adapter.iter_records(lines))
        assert len(records) == 3
        assert [r.session_id for r in records] == ["s0", "s1", "s2"]

    def test_drops_unrelated_lines_silently(self):
        """A stream interleaving cost-callback lines with raw LiteLLM
        INFO logs / blanks / truncated lines must only emit
        cost-callback records, never raise."""
        lines = [
            "",
            "INFO: starting litellm",
            _emit_line(session_id="s0"),
            '{"component": "litellm_router", "msg": "x"}',
            _emit_line(session_id="s1"),
            "{malformed",
        ]
        records = list(adapter.iter_records(lines))
        assert [r.session_id for r in records] == ["s0", "s1"]


# ─── read_tee_file() ──────────────────────────────────────────────────────


class TestReadTeeFile:
    def test_reads_from_path_str(self, tmp_path: Path):
        log = tmp_path / "litellm.log"
        log.write_text(
            "INFO: startup\n"
            + _emit_line(session_id="s0")
            + "\n"
            + _emit_line(session_id="s1")
            + "\n"
        )
        records = list(adapter.read_tee_file(str(log)))
        assert [r.session_id for r in records] == ["s0", "s1"]

    def test_reads_from_path_object(self, tmp_path: Path):
        log = tmp_path / "litellm.log"
        log.write_text(_emit_line(session_id="x") + "\n")
        records = list(adapter.read_tee_file(log))
        assert len(records) == 1
        assert records[0].session_id == "x"

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            list(adapter.read_tee_file(tmp_path / "does-not-exist.log"))

    def test_empty_file_yields_no_records(self, tmp_path: Path):
        log = tmp_path / "empty.log"
        log.write_text("")
        records = list(adapter.read_tee_file(log))
        assert records == []

    def test_decode_errors_replace(self, tmp_path: Path):
        """The adapter opens with ``errors='replace'`` so a binary
        stray byte in a long-running pod log doesn't kill the stream
        mid-measurement."""
        log = tmp_path / "with-bin.log"
        good = (_emit_line(session_id="s0") + "\n").encode("utf-8")
        log.write_bytes(b"\xff\xfe garbage \n" + good)
        records = list(adapter.read_tee_file(log))
        assert len(records) == 1
        assert records[0].session_id == "s0"


# ─── read_kubectl_logs() ──────────────────────────────────────────────────


class TestReadKubectlLogs:
    def test_kubectl_missing_raises_filenotfound(self, monkeypatch):
        """If neither the explicit ``kubectl_bin`` nor a PATH entry
        resolves, the adapter raises FileNotFoundError with the
        actionable docstring message — NOT a generic OSError that
        would mask the real cause."""
        monkeypatch.setattr(adapter.shutil, "which", lambda _name: None)
        with pytest.raises(FileNotFoundError, match="kubectl"):
            list(adapter.read_kubectl_logs("egg-litellm"))

    def test_invokes_kubectl_with_expected_argv(self, monkeypatch, tmp_path: Path):
        """The adapter must build a stable, shell-unsafe-free argv:
        ``kubectl logs deployment/<name> [-n <ns>] [--since <since>]``."""
        captured: dict[str, list[str]] = {}

        class _FakeProc:
            def __init__(self, args, **kwargs):
                captured["argv"] = args
                self.stdout = StringIO(
                    _emit_line(session_id="s-cluster") + "\n"
                )

            def terminate(self) -> None:
                pass

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                pass

        monkeypatch.setattr(adapter.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(adapter.shutil, "which", lambda _name: "/usr/bin/kubectl")

        records = list(
            adapter.read_kubectl_logs(
                "egg-litellm", namespace="kube-system", since="10m"
            )
        )
        assert records[0].session_id == "s-cluster"
        # Argv is exactly what the docstring promises.
        assert captured["argv"] == [
            "/usr/bin/kubectl",
            "logs",
            "deployment/egg-litellm",
            "-n",
            "kube-system",
            "--since",
            "10m",
        ]

    def test_default_deployment_and_no_optional_args(self, monkeypatch):
        captured: dict[str, list[str]] = {}

        class _FakeProc:
            def __init__(self, args, **kwargs):
                captured["argv"] = args
                self.stdout = StringIO("")

            def terminate(self) -> None:
                pass

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                pass

        monkeypatch.setattr(adapter.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(adapter.shutil, "which", lambda _name: "/usr/bin/kubectl")

        list(adapter.read_kubectl_logs())
        assert captured["argv"] == [
            "/usr/bin/kubectl",
            "logs",
            "deployment/egg-litellm",
        ]

    def test_terminates_subprocess_on_iter_end(self, monkeypatch):
        """The adapter must call ``terminate()`` after the generator is
        exhausted so the spawned ``kubectl logs`` doesn't leak."""
        events: list[str] = []

        class _FakeProc:
            def __init__(self, args, **kwargs):
                self.stdout = StringIO(_emit_line() + "\n")

            def terminate(self) -> None:
                events.append("terminate")

            def wait(self, timeout: float | None = None) -> int:
                events.append("wait")
                return 0

            def kill(self) -> None:
                events.append("kill")

        monkeypatch.setattr(adapter.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(adapter.shutil, "which", lambda _name: "/usr/bin/kubectl")
        list(adapter.read_kubectl_logs("egg-litellm"))
        assert "terminate" in events
        assert "wait" in events

    def test_kills_subprocess_when_wait_times_out(self, monkeypatch):
        events: list[str] = []

        class _FakeProc:
            def __init__(self, args, **kwargs):
                self.stdout = StringIO("")

            def terminate(self) -> None:
                events.append("terminate")

            def wait(self, timeout: float | None = None) -> int:
                events.append("wait")
                raise subprocess.TimeoutExpired(cmd="kubectl", timeout=timeout)

            def kill(self) -> None:
                events.append("kill")

        monkeypatch.setattr(adapter.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(adapter.shutil, "which", lambda _name: "/usr/bin/kubectl")
        list(adapter.read_kubectl_logs("egg-litellm"))
        assert events == ["terminate", "wait", "kill"]


# ─── summarise() ─────────────────────────────────────────────────────────


class TestSummarise:
    def test_aggregates_token_counts(self):
        records = [
            adapter.parse_record(
                _emit_line(prompt_tokens=1000, cached_tokens=600, cache_write_tokens=100)
            ),
            adapter.parse_record(
                _emit_line(prompt_tokens=500, cached_tokens=400, cache_write_tokens=50)
            ),
        ]
        summary = adapter.summarise(r for r in records if r is not None)
        assert summary["calls"] == 2
        assert summary["prompt_tokens"] == 1500
        assert summary["cache_read_tokens"] == 1000
        assert summary["cache_creation_tokens"] == 150

    def test_total_cost_none_when_all_costs_unknown(self):
        """Mirror the cost_callback policy: when no call has a known
        cost, the total must be ``None`` — never coerced to 0.0 (the
        whole reason cost_callback ships in the first place)."""
        records = [
            adapter.parse_record(_emit_line(cost=None)),
            adapter.parse_record(_emit_line(cost=None)),
        ]
        summary = adapter.summarise(r for r in records if r is not None)
        assert summary["cost_known_calls"] == 0
        assert summary["total_cost"] is None

    def test_total_cost_only_sums_known(self):
        records = [
            adapter.parse_record(_emit_line(cost=0.01)),
            adapter.parse_record(_emit_line(cost=None)),
            adapter.parse_record(_emit_line(cost=0.02)),
        ]
        summary = adapter.summarise(r for r in records if r is not None)
        assert summary["calls"] == 3
        assert summary["cost_known_calls"] == 2
        assert summary["total_cost"] == pytest.approx(0.03)

    def test_empty_records_returns_zero_totals(self):
        summary = adapter.summarise(iter([]))
        assert summary == {
            "calls": 0,
            "prompt_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cost_known_calls": 0,
            "total_cost": None,
        }


# ─── Cross-source equivalence (the key AC) ───────────────────────────────


class TestSourceEquivalence:
    """The slice-1 TASK-1-3 AC: *both source paths return the same
    ``{prompt_tokens, cache_read_tokens, cache_creation_tokens}``
    payload shape*. This class is the regression seat for that
    promise — if either source drifts, this test catches it."""

    def test_tee_and_kubectl_return_same_payload_shape(self, monkeypatch, tmp_path: Path):
        line = _emit_line(
            session_id="parity",
            prompt_tokens=2048,
            cached_tokens=1500,
            cache_write_tokens=42,
        )

        # Source A: stdout-tee file.
        log = tmp_path / "tee.log"
        log.write_text(line + "\n")
        tee_records = list(adapter.read_tee_file(log))

        # Source B: kubectl logs (mocked Popen with the same line).
        class _FakeProc:
            def __init__(self, args, **kwargs):
                self.stdout = StringIO(line + "\n")

            def terminate(self) -> None:
                pass

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                pass

        monkeypatch.setattr(adapter.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr(adapter.shutil, "which", lambda _name: "/usr/bin/kubectl")
        kubectl_records = list(adapter.read_kubectl_logs("egg-litellm"))

        # Both lists must be a single, identical CostRecord.
        assert len(tee_records) == 1
        assert len(kubectl_records) == 1
        assert tee_records[0] == kubectl_records[0]
        # AC payload-shape promise: matches verbatim across sources.
        assert tee_records[0].as_payload() == kubectl_records[0].as_payload()
        assert tee_records[0].as_payload() == {
            "model": "openrouter/qwen/qwen3-max",
            "prompt_tokens": 2048,
            "cache_read_tokens": 1500,
            "cache_creation_tokens": 42,
        }


# ─── Forbidden content (operator iteration-1 directive) ──────────────────


class TestNoForbiddenContent:
    """The plan AC explicitly bans TTL-bracket / idle-injection /
    stop-go-gate **code** from the adapter — narrative mentions
    (in docstrings explaining what the adapter is NOT) are fine and
    are actually load-bearing context for future maintainers. Encode
    the ban as structural tests: a synthetic idle is implemented as
    ``time.sleep``; bucketing logic ships as a function whose name
    contains ``bracket``/``bucket``/``ttl_gate``/``keep_warm``. Catch
    those, not the prose."""

    def test_no_time_sleep_in_adapter(self):
        """A synthetic idle would be implemented as ``time.sleep`` —
        the AC forbids running synthetic idle in this task. Allow
        ``time`` as a transitive (e.g. dataclass timestamps) but bar
        ``time.sleep`` and any explicit ``import time``."""
        source = _ADAPTER_PATH.read_text()
        assert "time.sleep" not in source, (
            "synthetic idle via time.sleep is forbidden by iteration-1"
        )
        assert "\nimport time\n" not in source, (
            "the adapter has no need for the ``time`` module — its presence "
            "suggests a synthetic-idle smuggle"
        )

    def test_public_api_has_no_bucketing_or_keepwarm_symbols(self):
        """The exported surface (``__all__``) must not include any
        function named after the banned behaviours. Catches the
        cheap mistake of pulling a helper from a previous draft back in."""
        banned = ("bracket", "bucket", "ttl_gate", "keep_warm", "keep_alive")
        for name in adapter.__all__:
            lowered = name.lower()
            for needle in banned:
                assert needle not in lowered, (
                    f"public symbol {name!r} contains banned token {needle!r}"
                )

    def test_no_bracket_or_bucket_function_defs(self):
        """Even non-exported helpers shouldn't carry the banned
        semantics. Scan the module dict for any callable whose name
        matches."""
        banned = ("bracket", "bucket", "ttl_gate", "keep_warm")
        for name, obj in vars(adapter).items():
            if not callable(obj):
                continue
            lowered = name.lower()
            for needle in banned:
                assert needle not in lowered, (
                    f"module-level callable {name!r} contains banned token {needle!r}"
                )


# ─── Public API surface (import contract for TASK-1-2 / TASK-9-1) ────────


class TestImportable:
    """The AC says the adapter must be *importable* from TASK-1-2's
    per-event measurement code AND from slice-9 TASK-9-1's integration
    test. We can't import the module name directly (leading digit), so
    the contract is: every name in ``__all__`` resolves to a callable
    or a type."""

    def test_all_symbols_resolve(self):
        for name in adapter.__all__:
            obj = getattr(adapter, name, None)
            assert obj is not None, f"missing public symbol: {name}"

    def test_all_includes_canonical_surface(self):
        """The AC names ``parse_record``, ``iter_records``,
        ``read_tee_file``, ``read_kubectl_logs``, ``summarise`` and the
        ``CostRecord`` dataclass as the consumer surface — lock that
        down explicitly."""
        assert set(adapter.__all__) >= {
            "CostRecord",
            "parse_record",
            "iter_records",
            "read_tee_file",
            "read_kubectl_logs",
            "summarise",
        }

    def test_costrecord_payload_shape_is_stable(self):
        """The dataclass fields are the consumer ABI — locking them
        down ensures a future field rename in the adapter forces a
        deliberate sweep of TASK-1-2 and TASK-9-1."""
        rec = adapter.parse_record(_emit_line())
        assert rec is not None
        expected_fields = {
            "timestamp",
            "session_id",
            "model",
            "cost",
            "prompt_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
        }
        # dataclass instances expose fields via __dataclass_fields__.
        assert set(rec.__dataclass_fields__.keys()) == expected_fields


# ─── CLI entry point (smoke) ─────────────────────────────────────────────


class TestCliSmoke:
    def test_cli_tee_emits_records_and_summary(self, tmp_path: Path, capsys):
        log = tmp_path / "litellm.log"
        log.write_text(
            _emit_line(session_id="s0") + "\n"
            + _emit_line(session_id="s1") + "\n"
        )
        rc = adapter._main(["--tee", str(log)])
        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        # Two record lines + one summary line.
        assert len(out) == 3
        first = json.loads(out[0])
        assert set(first.keys()) == {
            "model",
            "prompt_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        }
        last = json.loads(out[-1])
        assert last.get("summary") is True
        assert last["calls"] == 2

    def test_cli_requires_a_source(self, capsys):
        """``--tee`` and ``--kubectl`` are mutually exclusive AND one is
        required — a bare invocation must exit with a non-zero code
        and a usage message."""
        with pytest.raises(SystemExit) as exc:
            adapter._main([])
        # argparse exits with code 2 on usage errors.
        assert exc.value.code == 2
