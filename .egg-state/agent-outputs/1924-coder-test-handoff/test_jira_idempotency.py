"""
Tests for ``gateway/jira_idempotency.py``.

Covers TASK-5-1 acceptance criteria:

- Cache hit: same ``(verb, project, key)`` triple returns cached response,
  ``fn`` invoked exactly once.
- TTL expiry: an entry past ``IDEMPOTENCY_TTL_SECONDS`` is dropped on
  lookup and ``fn`` runs again.  We inject ``time.monotonic`` so the
  test does not actually sleep.
- Distinct keys: same verb + project, different ``key`` → distinct cache
  entries.
- Distinct verbs sharing keys: same opaque ``key`` against different
  verbs → distinct entries.
- ``key=None`` bypasses cache entirely.
- ``canonical_link_id`` produces stable, unique strings per triple.
- Thread-safety: concurrent calls for different keys do not deadlock and
  produce the expected number of upstream invocations.
"""

from __future__ import annotations

import threading
from typing import Any

import jira_idempotency
import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    jira_idempotency.clear_cache()
    yield
    jira_idempotency.clear_cache()


def _ok(payload: dict[str, Any], status: int = 200):
    """Build a zero-arg callable that returns ``(status, payload)`` and counts hits."""
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        return status, payload

    return fn, state


class TestGetOrRun:
    def test_cache_hit_returns_cached_value(self):
        fn, state = _ok({"id": "1"})
        first = jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        second = jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        assert first == (200, {"id": "1"})
        assert second == (200, {"id": "1"})
        assert state["calls"] == 1, "fn must run exactly once on cache hit"

    def test_distinct_keys_distinct_entries(self):
        fn, state = _ok({"id": "x"})
        jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        jira_idempotency.get_or_run("create", "ENG", "k2", fn)
        assert state["calls"] == 2

    def test_distinct_verbs_share_key_independent(self):
        fn, state = _ok({"id": "x"})
        jira_idempotency.get_or_run("create", "ENG", "shared", fn)
        jira_idempotency.get_or_run("comment", "ENG", "shared", fn)
        jira_idempotency.get_or_run("link", "ENG", "shared", fn)
        assert state["calls"] == 3

    def test_distinct_projects_share_key_independent(self):
        fn, state = _ok({"id": "x"})
        jira_idempotency.get_or_run("create", "ENG", "shared", fn)
        jira_idempotency.get_or_run("create", "DEVOPS", "shared", fn)
        assert state["calls"] == 2

    def test_none_key_bypasses_cache(self):
        fn, state = _ok({"id": "x"})
        jira_idempotency.get_or_run("create", "ENG", None, fn)
        jira_idempotency.get_or_run("create", "ENG", None, fn)
        assert state["calls"] == 2, "key=None must always re-run the upstream"

    def test_ttl_expiry_runs_again(self, monkeypatch: pytest.MonkeyPatch):
        """An entry past TTL is dropped on lookup; ``fn`` runs again."""
        clock = {"now": 1000.0}

        def fake_now():
            return clock["now"]

        monkeypatch.setattr(jira_idempotency, "_now", fake_now)

        fn, state = _ok({"id": "x"})
        jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        # Advance just shy of TTL — still cached.
        clock["now"] += jira_idempotency.IDEMPOTENCY_TTL_SECONDS - 1
        jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        assert state["calls"] == 1
        # Cross the TTL boundary.
        clock["now"] += 2
        jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        assert state["calls"] == 2

    def test_failure_not_cached(self):
        """Exceptions from ``fn`` must not poison the cache — the next retry runs again."""
        attempts = {"n": 0}

        class Boom(RuntimeError):
            pass

        def fn():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise Boom("upstream failure")
            return 200, {"id": "ok"}

        with pytest.raises(Boom):
            jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        result = jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        assert result == (200, {"id": "ok"})
        assert attempts["n"] == 2

    def test_status_code_round_trips(self):
        """The cached entry includes the upstream status code so the route layer
        can replay it verbatim."""
        fn, _ = _ok({"id": "x"}, status=201)
        first = jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        second = jira_idempotency.get_or_run("create", "ENG", "k1", fn)
        assert first == (201, {"id": "x"})
        assert second == (201, {"id": "x"})


class TestCanonicalLinkId:
    def test_stable_format(self):
        assert (
            jira_idempotency.canonical_link_id("ENG-1", "ENG-2", "Blocks")
            == "ENG-1|ENG-2|Blocks"
        )

    def test_distinct_triples_distinct_ids(self):
        a = jira_idempotency.canonical_link_id("ENG-1", "ENG-2", "Blocks")
        b = jira_idempotency.canonical_link_id("ENG-2", "ENG-1", "Blocks")
        c = jira_idempotency.canonical_link_id("ENG-1", "ENG-2", "Relates")
        assert len({a, b, c}) == 3, "every distinct triple yields a distinct id"

    def test_link_aliasing_safety(self):
        """Two callers re-using the same opaque idempotency key against different
        ``(inward, outward, type)`` triples must NOT alias to the same entry."""
        fn, state = _ok({"id": "x"})
        ns_a = jira_idempotency.canonical_link_id("ENG-1", "ENG-2", "Blocks")
        ns_b = jira_idempotency.canonical_link_id("ENG-3", "ENG-4", "Blocks")
        jira_idempotency.get_or_run("link", ns_a, "shared-key", fn)
        jira_idempotency.get_or_run("link", ns_b, "shared-key", fn)
        assert state["calls"] == 2


class TestThreadSafety:
    def test_distinct_keys_no_deadlock(self):
        """Spinning 16 threads against distinct keys produces 16 upstream calls
        and does not deadlock."""
        fn, state = _ok({"id": "x"})

        def hit(i: int):
            jira_idempotency.get_or_run("create", "ENG", f"k{i}", fn)

        threads = [threading.Thread(target=hit, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "thread did not finish — deadlock?"
        assert state["calls"] == 16
