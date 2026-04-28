"""
Tests for ``gateway/jira_idempotency.py``.

Covers:

- Cache miss → ``fn`` invoked once and result cached.
- Cache hit → ``fn`` is **not** invoked; cached value replayed verbatim.
- TTL expiry — once ``time.monotonic()`` advances past
  ``IDEMPOTENCY_TTL_SECONDS`` the entry is dropped and ``fn`` runs again.
- Distinct ``key`` values → distinct entries (no collision).
- Distinct ``verb`` values sharing the same ``(project, key)`` → distinct
  entries (verbs are first in the cache key tuple).
- ``key=None`` / ``key=""`` bypasses the cache entirely (no dedup).
- Threading: concurrent miss/miss with the same key → ``fn`` may run twice
  (we run ``fn`` outside the lock to avoid serialising upstream calls — see
  the module docstring), and **the cache state remains consistent** under
  contention.  The link-cache aliasing test below verifies that when the
  caller chooses different synthetic projects (``A__B__Blocks`` vs
  ``B__A__Blocks``) the same opaque key produces distinct entries.
- ``clear_cache()`` wipes everything.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# Module loaded via conftest's _load_module_with_replaced_imports trick is
# only used for already-pre-registered modules; jira_idempotency is a new
# module added in #1924, so we import it directly.  ``conftest.py`` puts
# the gateway directory on ``sys.path`` indirectly via ``GATEWAY_DIR`` —
# we rely on that.
import jira_idempotency
import pytest
from jira_idempotency import IDEMPOTENCY_TTL_SECONDS, clear_cache, get_or_run


@pytest.fixture(autouse=True)
def _wipe_cache_between_tests():
    """Idempotency cache is module-level; reset before and after each test."""
    clear_cache()
    yield
    clear_cache()


# -----------------------------------------------------------------------------
# Basic miss / hit semantics
# -----------------------------------------------------------------------------


class TestCacheMissAndHit:
    def test_miss_invokes_fn_and_caches(self):
        calls: list[int] = []

        def fn() -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 201, {"key": "ENG-1", "id": "10001"}

        status, body = get_or_run("jira_ticket_create", "ENG", "key-1", fn)
        assert status == 201
        assert body == {"key": "ENG-1", "id": "10001"}
        assert calls == [1]

    def test_hit_does_not_invoke_fn(self):
        calls: list[int] = []

        def fn() -> tuple[int, dict[str, Any]]:
            calls.append(len(calls) + 1)
            return 201, {"key": f"ENG-{len(calls)}"}

        # Prime the cache.
        first = get_or_run("jira_ticket_create", "ENG", "key-1", fn)
        # Replay should not invoke fn again.
        second = get_or_run("jira_ticket_create", "ENG", "key-1", fn)

        assert first == second
        assert calls == [1], f"fn invoked more than once: {calls}"

    def test_hit_replays_response_verbatim(self):
        """Cache replays the EXACT object that was stored — body identity
        is preserved (callers depend on response shape stability)."""
        captured: dict[str, Any] = {"key": "ENG-1", "labels": ["foo"]}

        def fn() -> tuple[int, dict[str, Any]]:
            return 201, captured

        get_or_run("v", "ENG", "k", fn)
        _, replayed = get_or_run("v", "ENG", "k", fn)
        # Same dict reference (not a deep copy) — Atlassian responses are
        # treated as opaque payloads by the route layer.
        assert replayed is captured


# -----------------------------------------------------------------------------
# Bypass paths
# -----------------------------------------------------------------------------


class TestBypass:
    @pytest.mark.parametrize("missing_key", [None, ""])
    def test_falsy_key_bypasses_cache(self, missing_key):
        calls: list[int] = []

        def fn() -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 201, {"k": "v"}

        # Two calls with no key — fn must run twice.
        get_or_run("jira_ticket_create", "ENG", missing_key, fn)
        get_or_run("jira_ticket_create", "ENG", missing_key, fn)
        assert calls == [1, 1]


# -----------------------------------------------------------------------------
# TTL expiry
# -----------------------------------------------------------------------------


class TestTtlExpiry:
    def test_stale_entry_evicted_and_fn_re_runs(self, monkeypatch):
        """Advance the monotonic clock past the TTL; the next lookup should
        miss and re-invoke ``fn``."""
        # Seed a deterministic clock starting at t=1000.
        clock = {"now": 1000.0}
        monkeypatch.setattr(jira_idempotency.time, "monotonic", lambda: clock["now"])

        calls: list[int] = []

        def fn() -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 201, {"call_number": len(calls)}

        # Insert at t=1000.
        first = get_or_run("v", "P", "k", fn)
        assert first[1]["call_number"] == 1

        # Advance past the TTL.
        clock["now"] = 1000.0 + IDEMPOTENCY_TTL_SECONDS + 0.001
        second = get_or_run("v", "P", "k", fn)
        assert second[1]["call_number"] == 2
        assert calls == [1, 1]

    def test_entry_within_ttl_still_hits(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(jira_idempotency.time, "monotonic", lambda: clock["now"])

        calls: list[int] = []

        def fn() -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 201, {"x": 1}

        get_or_run("v", "P", "k", fn)
        clock["now"] = 1000.0 + IDEMPOTENCY_TTL_SECONDS - 1
        get_or_run("v", "P", "k", fn)
        assert calls == [1]

    def test_ttl_constant_matches_5_min(self):
        # Refine decision-16 pinned the TTL at 5 minutes.  If a future
        # change widens the window, the test alerts the maintainer.
        assert IDEMPOTENCY_TTL_SECONDS == 5 * 60


# -----------------------------------------------------------------------------
# Distinct keys / verbs / projects
# -----------------------------------------------------------------------------


class TestKeyspace:
    def test_distinct_keys_dont_collide(self):
        calls: list[str] = []

        def make_fn(name: str):
            def fn() -> tuple[int, dict[str, Any]]:
                calls.append(name)
                return 201, {"who": name}

            return fn

        a = get_or_run("v", "P", "key-a", make_fn("A"))
        b = get_or_run("v", "P", "key-b", make_fn("B"))
        assert a[1]["who"] == "A"
        assert b[1]["who"] == "B"
        assert calls == ["A", "B"]

    def test_distinct_verbs_share_keyspace(self):
        """Two distinct verbs sharing the same opaque key are distinct
        entries — the key tuple is ``(verb, project, key)``."""
        calls: list[str] = []

        def make_fn(name: str):
            def fn() -> tuple[int, dict[str, Any]]:
                calls.append(name)
                return 201, {"who": name}

            return fn

        a = get_or_run("jira_ticket_create", "P", "k", make_fn("create"))
        b = get_or_run("jira_comment_add", "P", "k", make_fn("comment"))
        # Different verbs → different cache slots; both fns ran.
        assert a[1]["who"] == "create"
        assert b[1]["who"] == "comment"
        assert calls == ["create", "comment"]

    def test_distinct_projects_dont_collide(self):
        calls: list[str] = []

        def make_fn(name: str):
            def fn() -> tuple[int, dict[str, Any]]:
                calls.append(name)
                return 201, {"who": name}

            return fn

        get_or_run("v", "ENG", "k", make_fn("ENG"))
        get_or_run("v", "DEVOPS", "k", make_fn("DEVOPS"))
        assert calls == ["ENG", "DEVOPS"]


# -----------------------------------------------------------------------------
# clear_cache
# -----------------------------------------------------------------------------


class TestClearCache:
    def test_clear_cache_evicts_all(self):
        calls: list[int] = []

        def fn() -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 201, {"x": 1}

        get_or_run("v", "P", "k", fn)
        get_or_run("v", "P", "k", fn)  # hit
        assert calls == [1]

        clear_cache()

        get_or_run("v", "P", "k", fn)  # miss again after clear
        assert calls == [1, 1]


# -----------------------------------------------------------------------------
# Concurrency — race-safety
# -----------------------------------------------------------------------------


class TestConcurrency:
    def test_thread_safe_cache_under_contention(self):
        """Many threads racing on the same (verb, project, key) must all
        receive a valid response, the cache must end in a consistent state,
        and ``fn`` must run at least once but not crash.

        Per the module docstring, ``fn`` runs OUTSIDE the lock — concurrent
        misses can both invoke ``fn`` (the last one to finish wins the cache
        slot).  We assert:

        - Every thread sees a valid (status, body) tuple (no exceptions).
        - All threads converge to the SAME cached value in the end.
        - The number of ``fn`` invocations is bounded (>=1, <= n_threads).
        """
        n_threads = 32
        results: list[tuple[int, dict[str, Any]]] = []
        results_lock = threading.Lock()
        run_count = {"n": 0}
        run_count_lock = threading.Lock()

        def fn() -> tuple[int, dict[str, Any]]:
            with run_count_lock:
                run_count["n"] += 1
                # Stable response — concurrent misses get the same payload
                # regardless of which thread "wins".
            time.sleep(0.001)  # encourage interleaving
            return 201, {"value": "stable"}

        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            res = get_or_run("v", "P", "race-key", fn)
            with results_lock:
                results.append(res)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        assert len(results) == n_threads
        # All callers see the SAME stable response.
        for status, body in results:
            assert status == 201
            assert body == {"value": "stable"}
        # ``fn`` ran at least once, and no more than n_threads times.
        assert 1 <= run_count["n"] <= n_threads

        # Once the dust settles, a fresh call should hit the cache (no new
        # ``fn`` invocations).  Sample the run count, then call.
        before = run_count["n"]
        get_or_run("v", "P", "race-key", fn)
        assert run_count["n"] == before, "post-contention call did not hit cache"


# -----------------------------------------------------------------------------
# Link-cache aliasing — same opaque key against different (inward, outward,
# type) triples must produce distinct cache entries.
#
# This mirrors what JiraClient.create_issue_link does: it derives a
# synthetic project tag of the form ``f"{inward}__{outward}__{link_type}"``
# and feeds that to ``get_or_run``.  This test verifies the cache keys on
# THAT triple — the module under test is keyspace-agnostic, so a colliding
# synthetic-project string would collapse two different links into the same
# cache slot.
# -----------------------------------------------------------------------------


class TestLinkCacheAliasing:
    def test_same_key_different_synthetic_projects_distinct(self):
        """A caller that uses the same opaque ``idempotency_key`` against
        two different ``(inward, outward, type)`` triples must see two
        distinct cache entries — verbs/projects/keys are jointly the key."""
        calls: list[str] = []

        def make_fn(label: str):
            def fn() -> tuple[int, dict[str, Any]]:
                calls.append(label)
                return 201, {"label": label}

            return fn

        # Same opaque key, two different synthetic projects (matches the
        # ``inward__outward__type`` shape JiraClient builds).
        a = get_or_run(
            "jira_issue_link_create",
            "ENG-1__ENG-2__Blocks",
            "k",
            make_fn("triple-A"),
        )
        b = get_or_run(
            "jira_issue_link_create",
            "ENG-1__ENG-3__Blocks",
            "k",
            make_fn("triple-B"),
        )
        assert a[1]["label"] == "triple-A"
        assert b[1]["label"] == "triple-B"
        assert calls == ["triple-A", "triple-B"]

    def test_same_triple_same_key_dedupes(self):
        """Sanity check the corollary: same key + same synthetic project →
        a single cache entry (this is the desired retry-dedup behaviour)."""
        calls: list[str] = []

        def make_fn(label: str):
            def fn() -> tuple[int, dict[str, Any]]:
                calls.append(label)
                return 201, {"label": label}

            return fn

        get_or_run(
            "jira_issue_link_create",
            "ENG-1__ENG-2__Blocks",
            "retry-key",
            make_fn("first"),
        )
        get_or_run(
            "jira_issue_link_create",
            "ENG-1__ENG-2__Blocks",
            "retry-key",
            make_fn("second"),  # never called — replay first
        )
        assert calls == ["first"]

    def test_a_to_b_and_b_to_a_are_distinct_links(self):
        """Direction matters: A→B Blocks and B→A Blocks are different links
        in Atlassian, and must occupy different cache slots even when the
        caller (mistakenly) reuses the same opaque key."""
        calls: list[str] = []

        def make_fn(label: str):
            def fn() -> tuple[int, dict[str, Any]]:
                calls.append(label)
                return 201, {"label": label}

            return fn

        get_or_run(
            "jira_issue_link_create",
            "ENG-1__ENG-2__Blocks",
            "k",
            make_fn("A->B"),
        )
        get_or_run(
            "jira_issue_link_create",
            "ENG-2__ENG-1__Blocks",
            "k",
            make_fn("B->A"),
        )
        assert calls == ["A->B", "B->A"]


# -----------------------------------------------------------------------------
# Edge-cases on the cache key
# -----------------------------------------------------------------------------


class TestEdgeCases:
    def test_unicode_key_accepted(self):
        calls: list[int] = []

        def fn() -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 201, {"x": 1}

        get_or_run("v", "P", "key-with-héllo", fn)
        get_or_run("v", "P", "key-with-héllo", fn)
        assert calls == [1]

    def test_long_key_accepted(self):
        calls: list[int] = []

        def fn() -> tuple[int, dict[str, Any]]:
            calls.append(1)
            return 201, {"x": 1}

        long_key = "k" * 4096
        get_or_run("v", "P", long_key, fn)
        get_or_run("v", "P", long_key, fn)
        assert calls == [1]
