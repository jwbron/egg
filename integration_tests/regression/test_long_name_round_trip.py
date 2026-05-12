"""k3s regression guard for long-Job-name round-trips (#2644).

``KubernetesClient.create_container`` truncates Job names > 63 chars
and appends an 8-char SHA digest. Before #2644 landed, ``delete_job``,
``read_namespaced_job``, and ``get_pod_for_job`` did NOT apply the
same truncation — they passed the un-truncated name straight to the
k8s API, which 404'd because the Job existed under the truncated
form.

This test pins the round-trip: create a Job with an input name long
enough to trigger truncation, then delete it via the same client.
A regression that re-introduces the asymmetry would leave the Job
in the namespace after the delete (no 404 propagated, but the Job
is still there), which the assertion catches.

The fix landed via ``KubernetesClient._normalize_k8s_job_name``
which both ``create_container`` and ``delete_job`` invoke.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


def test_long_name_create_then_delete_round_trips(egg_stack):
    """A Job created via ``create_container`` must be deletable via
    ``delete_job`` regardless of name length.
    """
    try:
        from kubernetes_client import KubernetesClient
    except ImportError as e:
        pytest.skip(f"Could not import KubernetesClient: {e}")

    namespace = egg_stack.isolated_network
    client = KubernetesClient(namespace=namespace)

    # Construct an input name long enough that ``create_container``
    # MUST truncate. The prefix it adds is ``egg-sandbox-`` (12 chars),
    # the validator caps at 63, so any input ≥ 52 chars triggers
    # truncation. Use 58 chars of distinct content to keep the SHA
    # digest a meaningful uniqueness anchor.
    long_input = "long-name-regression-2644-aaaa-bbbb-cccc-dddd-eeee-ffff-gg"
    assert len(long_input) >= 52, "Test input must trigger truncation"

    spawned = client.create_container(
        name=long_input,
        environment={"EGG_FOO": "bar"},
    )
    actual_job_name = spawned.job_name
    assert actual_job_name != f"egg-sandbox-{long_input}", (
        "Test input did not trigger truncation — pick a longer name"
    )

    try:
        # The delete the orchestrator's restart path issues today: it
        # passes the un-truncated form computed by name-builder helpers.
        client.delete_job(
            name=f"egg-sandbox-{long_input}",
            namespace=namespace,
            propagation_policy="Foreground",
        )

        # Poll for the Job to actually disappear from the API server.
        # If #2644 is unfixed the delete was a no-op (silent 404 inside
        # the client) and the Job lingers indefinitely.
        import time as _t

        deadline = _t.monotonic() + 30.0
        gone = False
        while _t.monotonic() < deadline:
            try:
                client.batch_api.read_namespaced_job(
                    name=actual_job_name,
                    namespace=namespace,
                )
            except Exception as exc:  # noqa: BLE001 — only care about 404
                if "not found" in str(exc).lower() or "404" in str(exc):
                    gone = True
                    break
            _t.sleep(0.5)
        assert gone, (
            f"Job {actual_job_name!r} survived the delete — "
            "delete_job is not applying the truncation that "
            "create_container does (see #2644)."
        )
    finally:
        # Best-effort cleanup via the actual (truncated) name, regardless
        # of test outcome, so re-runs don't pile up zombie Jobs.
        try:
            client.delete_job(
                name=actual_job_name,
                namespace=namespace,
                propagation_policy="Background",
            )
        except Exception:  # noqa: BLE001
            pass
