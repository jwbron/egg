# egg-redis — stock Redis, repackaged as a locally built/published image.
#
# Backs the orchestrator's Redis Streams message store
# (orchestrator/redis_message_store.py), the production backend for
# inter-agent messages (#2662). Durability target: the BRC transcript and
# consensus-tracker replay source must survive a mid-phase orchestrator
# restart (#3076) — so messages live here, not in the orchestrator process.
#
# All egg images are built locally and published to the cluster through the
# loopback registry / save+import flow (`make redeploy`, #2999); the cluster
# never pulls from public registries. This thin wrapper keeps Redis on that
# same supply chain instead of introducing the first public image reference
# in the manifests.
#
# Pinned to a specific upstream release; bump deliberately. The orchestrator
# uses Streams primitives only (XADD / XREAD BLOCK / XRANGE / XLEN / DEL) —
# stable since Redis 5 — so patch/minor bumps are low-risk, but keep the
# fakeredis-backed unit suite (orchestrator/tests/test_redis_message_store.py)
# and `make test-integration` green across any bump.
#
# No USER directive: the Deployment's securityContext
# (runAsUser/runAsGroup/fsGroup 1000, runAsNonRoot) governs the runtime UID,
# and the stock entrypoint works under an arbitrary non-root UID as long as
# /data is writable (fsGroup handles that).
FROM redis:8.6.4-alpine
