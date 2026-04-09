# Analysis: Migrate to Kubernetes

> Issue: #1553 | Phase: refine

## Problem Statement

The egg platform currently uses a three-tier Docker architecture where the orchestrator spawns agent containers via the Docker SDK. This design binds all agents to a single host, provides no native scheduling or fault tolerance, and leaves resource contention unmanaged. The issue requests migrating to Kubernetes (using k3s for local development) to enable multi-node scaling, proper scheduling, and automatic recovery.

The desired outcome is a fully working egg pipeline on k3s where `make k3s-setup && make deploy` replaces the current Docker Compose workflow, with no Docker dependencies remaining.

## Current Behavior

### Container Lifecycle

The orchestrator manages agent containers through three core modules (~2,400 lines total):

| Module | Lines | Responsibility |
|--------|-------|----------------|
| `orchestrator/docker_client.py` | 534 | Docker SDK wrapper: container CRUD, label-based listing, log retrieval, orphan cleanup. Singleton via `get_docker_client()`. Custom exception hierarchy (`DockerClientError`, `ContainerNotFoundError`, etc.). |
| `orchestrator/container_spawner.py` | 988 | Full agent lifecycle: gateway session registration, per-agent worktree creation, repo volume mounts with `.git` shadow binding, phase-based readonly enforcement, dual-network attachment with static/dynamic IP allocation, 30+ env vars per agent, post-exit uncommitted change detection. |
| `orchestrator/container_monitor.py` | 884 | Background health polling (10s interval), event-driven state callbacks (STARTED/STOPPED/EXITED/FAILED/UNHEALTHY), orphan cleanup via reconciliation. |

Additional Docker-dependent code:
- `orchestrator/concurrent_executor.py` (457 lines) — multi-agent phase orchestration via `spawn_fn` callbacks
- `sandbox/egg_lib/runtime.py` (1,197 lines) — CLI-side container exec, session management, IP allocation via `build_sandbox_docker_cmd()`

### Networking

Two Docker networks provide isolation:
- **`egg-isolated`** (172.32.0.0/24, `internal: true`): Private mode — no external gateway, all traffic forced through Squid proxy on gateway
- **`egg-external`** (172.33.0.0/24, bridged): Public mode — direct internet via gateway proxy

### Deployment Infrastructure

- **Docker Compose**: `docker-compose.yml` (199 lines) — gateway + orchestrator as long-lived services
- **Integration tests**: `integration_tests/docker-compose.yml` (76 lines) — test-only gateway
- **Local pipeline tests**: `integration_tests/local_pipeline/docker-compose.yml` (126 lines) — full stack with mock sandbox

## Constraints

### Technical
- Network isolation is security-critical — Calico CNI required (Flannel doesn't support NetworkPolicies)
- Per-agent worktree isolation must be preserved
- Docker socket dependency replaced with k8s API access (ServiceAccount + RBAC)
- Token-only gateway auth (remove IP binding)

### Business
- Full cutover (Option A) — no dual-backend
- Local k3s only (GKE deferred)
- DevserverManager already removed in #1558

## Recommended Approach

Option A: Full Cutover with ContainerBackend Protocol for testability. k8s Jobs for agents, Calico NetworkPolicies, Kustomize overlays, token-only gateway auth.

## Complexity Assessment

High — fundamental architectural change affecting core container lifecycle, CLI, deployment infra, network isolation, storage, CI/CD, and developer experience.