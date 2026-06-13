"""End-to-end coverage for the deployed message-store backend (#2662).

The k8s manifests pin the orchestrator to the Redis Streams backend
(``EGG_MESSAGE_STORE_BACKEND=redis`` + the ``redis`` Deployment/Service in
``k8s/base/``). Before #2662 the cluster deployed no Redis, so production
silently ran the (since-removed, #3159) in-memory ``MessageStore`` and
``RedisMessageStore`` had zero live coverage — everything above unit tier
went through ``fakeredis``.

These tests run against the live cluster ``make test-integration`` deploys:

* the deployed orchestrator explicitly pins the ``redis`` backend, and
* a real XADD/XRANGE/DEL round-trip works from inside the orchestrator
  container, through the production creation logic in
  ``message_store._create_message_store()`` — exercising the env wiring,
  the in-image redis client, Service DNS, and the live Redis in one path.

The Streams semantics themselves (filters, blocking reads, cursor
staleness) are pinned by ``orchestrator/tests/test_redis_message_store.py``.
"""

import subprocess
import uuid

import requests

from integration_tests.conftest import EggStack

_NS = "egg-system"


def _kubectl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "-n", _NS, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class TestMessageStoreBackend:
    def test_deployed_backend_is_explicit_redis(self, egg_stack: EggStack) -> None:
        """The orchestrator Deployment pins EGG_MESSAGE_STORE_BACKEND=redis.

        Since #3159 redis is the only backend (unset also means redis,
        and the removed ``auto``/``memory`` values fail loudly at
        creation), but the manifest keeps the explicit pin as deployed
        documentation of the choice — a regression that drops or edits
        it fails here. The real end-to-end coverage that the pinned
        backend actually works lives in
        ``test_redis_streams_roundtrip_from_orchestrator_pod``.
        """
        result = _kubectl(
            "get",
            "deployment",
            "orchestrator",
            "-o",
            "jsonpath={.spec.template.spec.containers[0].env"
            "[?(@.name=='EGG_MESSAGE_STORE_BACKEND')].value}",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "redis", (
            "Deployed orchestrator must pin EGG_MESSAGE_STORE_BACKEND=redis; "
            f"got {result.stdout.strip()!r}. See #2662."
        )

    def test_health_carries_no_message_store_component(self, orchestrator_url: str) -> None:
        """/api/v1/health no longer reports a ``message_store`` component.

        The #3077 slice-6 degraded component existed solely to surface
        the auto→memory fallback; #3159 removed that fallback (and the
        in-memory backend) so creation fails loudly instead of degrading.
        This pins the surface removal — a reappearing component means
        fallback machinery grew back. The round-trip test below carries
        the real proof that the orchestrator→Redis path works.
        """
        resp = requests.get(f"{orchestrator_url}/api/v1/health", timeout=10)
        body = resp.json()
        components = body.get("components", {})
        assert "message_store" not in components, (
            f"unexpected message_store health component {components.get('message_store')!r} "
            "— the #3077 slice-6 fallback surface was removed in #3159."
        )

    def test_redis_streams_roundtrip_from_orchestrator_pod(self, egg_stack: EggStack) -> None:
        """Live XADD/XRANGE/DEL through the production selection path.

        Runs a fresh Python process inside the orchestrator container so
        ``get_message_store()`` re-runs backend selection with the pod's
        real env. In explicit ``redis`` mode any failure (missing client
        package, DNS, connection, AUTH) raises rather than falling back,
        so a passing run proves the orchestrator→Redis path end to end.
        """
        pipeline_id = f"itest-redis-{uuid.uuid4().hex[:8]}"
        script = (
            "from message_store import Message, get_message_store\n"
            "store = get_message_store()\n"
            f"pid = {pipeline_id!r}\n"
            "store.add_message(Message(pipeline_id=pid, from_role='itest',"
            " message_type='PROGRESS', body='redis-e2e'))\n"
            "msgs = store.get_messages(pid)\n"
            "assert [m.body for m in msgs] == ['redis-e2e'], msgs\n"
            "cleared = store.clear(pid)\n"
            "assert cleared == 1, cleared\n"
            "print('BACKEND', type(store).__name__)\n"
        )
        result = _kubectl(
            "exec",
            "deploy/orchestrator",
            "--",
            "python",
            "-c",
            script,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"in-pod round-trip failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "BACKEND RedisMessageStore" in result.stdout, (
            f"expected the RedisMessageStore backend, got: {result.stdout!r}"
        )
