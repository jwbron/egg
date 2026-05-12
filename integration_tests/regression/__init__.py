"""Regression tier for k3s integration tests.

Each module pins a specific invariant against the deployed orchestrator
+ gateway in the k3s overlay. The parent conftest's ``egg_stack`` and
``orchestrator_url`` fixtures auto-skip when ``kubectl`` is unavailable,
so a local ``make test`` without a cluster cleanly skips this whole
subtree.

See ``integration_tests/regression/conftest.py`` for the helpers
specific to this tier (lifecycle-secret lookup, ephemeral pipeline
ids, etc.) — the parent ``integration_tests/conftest.py`` still owns
the k3s harness.
"""
