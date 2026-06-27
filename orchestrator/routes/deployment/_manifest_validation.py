"""``validate_deployment_manifests`` — static kustomize-overlay linting (#3312).

Renders the committed kustomize overlay and applies the #1759 / #3005 / #3070
warning rules (Secret refs, hostPath presence, image tags, Service selectors,
env collisions, gateway/orchestrator persistent-store coupling). The kustomize
runner and k3s detector are reached via ``_pkg`` so the route tests'
``patch("routes.deployment._run_kustomize" / "._detect_k3s")`` seams — and the
``monkeypatch routes.deployment.subprocess.run`` seam — stay effective.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import routes.deployment as _pkg
import yaml
from flask import Response, jsonify, request

from ._runtime import _not_available_on_runtime

_DEFAULT_OVERLAY = "k8s/overlays/local"


def _run_kustomize(overlay_path: Path) -> list[dict[str, Any]]:
    """Render the overlay with ``kustomize build`` and return docs.

    Raises RuntimeError if kustomize fails or returns empty output.
    """
    exe = os.environ.get("EGG_KUSTOMIZE_BIN", "kustomize")
    try:
        proc = subprocess.run(
            [exe, "build", str(overlay_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except FileNotFoundError:
        # Fall back to ``kubectl kustomize``; some environments only
        # ship kubectl. If kubectl is also missing, surface a
        # structured error rather than a 500.
        try:
            proc = subprocess.run(
                ["kubectl", "kustomize", str(overlay_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "kustomize_unavailable: neither kustomize nor kubectl is on PATH"
            ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"kustomize build timed out: {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"kustomize build failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )

    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    if not docs:
        raise RuntimeError("kustomize build produced no documents")
    return docs


def _warn(
    warnings: list[dict[str, Any]],
    rule: str,
    severity: str,
    resource: str,
    message: str,
    **extra: Any,
) -> None:
    entry: dict[str, Any] = {
        "rule": rule,
        "severity": severity,
        "resource": resource,
        "message": message,
    }
    if extra:
        entry["extra"] = extra
    warnings.append(entry)


def _deployment_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = (doc.get("spec") or {}).get("template", {}).get("spec") or {}
    return list(spec.get("containers") or [])


def _deployment_volumes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = (doc.get("spec") or {}).get("template", {}).get("spec") or {}
    return list(spec.get("volumes") or [])


def _validate_deployment_docs(docs: list[dict[str, Any]], *, is_k3s: bool) -> list[dict[str, Any]]:
    """Apply the warning rules from the #1759 validation session.

    Rules:

    1. Missing ``Secret`` reference (secretName in a volume with no
       matching Secret resource in the overlay).
    2. Missing ``hostPath`` for gateway / orchestrator volumes on local
       overlays (skipped when we cannot detect an overlay with hostPath
       mounts at all — matching non-local deploys).
    3. Missing container image tag (``image:`` without ``:`` or with a
       placeholder ``:latest`` tag — k3s-gated because only k3s relies
       on locally-imported image tags).
    4. Service selector labels not matching deployment template labels.
    5. Env-var name collision: same name declared twice in a single
       container's ``env`` list.
    6. Gateway session store ephemeral while worktrees are persistent
       (#3005): an ephemeral ``egg-state`` (emptyDir, or absent so it falls
       inside the ``home`` emptyDir) paired with a persistent ``worktrees``
       lets a gateway pod recreation wipe live pipeline worktrees.
    """
    warnings: list[dict[str, Any]] = []

    # Build lookup tables
    secrets = {d.get("metadata", {}).get("name") for d in docs if d.get("kind") == "Secret"}
    deployments = [d for d in docs if d.get("kind") == "Deployment"]
    services = [d for d in docs if d.get("kind") == "Service"]

    any_hostpath = any(
        any("hostPath" in (v or {}) for v in _deployment_volumes(d)) for d in deployments
    )

    # Rule 1: Secret references
    for dep in deployments:
        name = dep.get("metadata", {}).get("name", "<unknown>")
        for vol in _deployment_volumes(dep):
            secret_cfg = vol.get("secret")
            if not secret_cfg:
                continue
            secret_name = secret_cfg.get("secretName")
            if secret_name and secret_name not in secrets:
                _warn(
                    warnings,
                    rule="secret-missing",
                    severity="error",
                    resource=f"Deployment/{name}",
                    message=(
                        f"volume references Secret '{secret_name}' which is not "
                        "declared in the overlay"
                    ),
                )

    # Rule 2: hostPath volume presence on local overlays.
    if any_hostpath:
        for dep in deployments:
            name = dep.get("metadata", {}).get("name", "<unknown>")
            if "gateway" not in name and "orchestrator" not in name:
                continue
            has_hostpath = any("hostPath" in (v or {}) for v in _deployment_volumes(dep))
            if not has_hostpath:
                _warn(
                    warnings,
                    rule="hostpath-missing",
                    severity="warn",
                    resource=f"Deployment/{name}",
                    message=(
                        "local overlay declares hostPath mounts elsewhere but this "
                        "Deployment has none — worktrees/repos will not be visible"
                    ),
                )

    # Rule 3: image tag presence (k3s-gated)
    if is_k3s:
        for dep in deployments:
            name = dep.get("metadata", {}).get("name", "<unknown>")
            for c in _deployment_containers(dep):
                image = (c or {}).get("image", "")
                if not image:
                    _warn(
                        warnings,
                        rule="image-missing",
                        severity="error",
                        resource=f"Deployment/{name}",
                        message=f"container '{c.get('name', '<unnamed>')}' has no image field",
                    )
                    continue
                if ":" not in image:
                    _warn(
                        warnings,
                        rule="image-missing-tag",
                        severity="warn",
                        resource=f"Deployment/{name}",
                        message=(
                            f"container image '{image}' has no tag — k3s "
                            "containerd will not find the locally-imported image"
                        ),
                    )
    else:
        warnings.append(
            {
                "skipped": "not_k3s",
                "rule": "image-missing-tag",
                "detected_runtime": None,
            }
        )

    # Rule 4: Service selector labels vs Deployment template labels
    for svc in services:
        svc_name = svc.get("metadata", {}).get("name", "<unknown>")
        selector = ((svc.get("spec") or {}).get("selector")) or {}
        if not selector:
            continue
        matched = False
        for dep in deployments:
            labels = (
                (dep.get("spec") or {}).get("template", {}).get("metadata", {}).get("labels", {})
            ) or {}
            if labels and all(labels.get(k) == v for k, v in selector.items()):
                matched = True
                break
        if not matched:
            _warn(
                warnings,
                rule="selector-label-mismatch",
                severity="warn",
                resource=f"Service/{svc_name}",
                message=(
                    f"service selector {selector!r} does not match any Deployment "
                    "template labels in the overlay"
                ),
            )

    # Rule 5: env-var name collision within a container
    for dep in deployments:
        name = dep.get("metadata", {}).get("name", "<unknown>")
        for c in _deployment_containers(dep):
            seen: dict[str, int] = {}
            for entry in (c or {}).get("env") or []:
                env_name = (entry or {}).get("name")
                if not env_name:
                    continue
                seen[env_name] = seen.get(env_name, 0) + 1
            dupes = [k for k, v in seen.items() if v > 1]
            for d in dupes:
                _warn(
                    warnings,
                    rule="env-var-collision",
                    severity="error",
                    resource=f"Deployment/{name}",
                    message=(
                        f"container '{c.get('name', '<unnamed>')}' declares env '{d}' "
                        "more than once"
                    ),
                )

    # Rule 6: gateway session store must share the worktrees' persistence
    # lifetime (#3005). When an overlay gives a gateway's worktrees a volume
    # that survives pod recreation, the gateway session store — the
    # ``egg-state`` volume mounted at /home/egg/.egg-state
    # (gateway/session_manager.py) — MUST also survive. The two are coupled:
    # startup worktree cleanup
    # (gateway/worktree_manager.py:cleanup_orphaned_worktrees) only protects a
    # live pipeline's worktrees if it can see the owning sessions (the #1874
    # session-anchor derivation). If the session store is ephemeral
    # (``emptyDir`` or absent — in which case /home/egg/.egg-state falls
    # inside the ``home`` emptyDir) while the worktrees survive on a
    # persistent volume, a gateway *pod* recreation boots with the worktrees
    # still on disk but zero sessions, so cleanup runs with
    # active_containers=0 and deletes every live worktree out from under its
    # running phase agents, deadlocking BRC consensus. The rule is expressed
    # against ``emptyDir`` (the ephemeral side) rather than ``hostPath`` (one
    # specific persistent backing) so it generalizes to PVC / NFS / CSI
    # backings a future cloud overlay might use — the real invariant is
    # "session store has at least the same persistence class as worktrees,"
    # not "both are hostPath." Self-gated on the worktrees actually being
    # persistent so it stays silent on all-emptyDir base/cloud deploys (where
    # nothing survives a pod recreation, so there is no asymmetry to exploit).
    for dep in deployments:
        name = dep.get("metadata", {}).get("name", "<unknown>")
        # Match the gateway deployment by exact name or ``gateway-*`` prefix
        # so the rule fires on canary / rollout variants but not on unrelated
        # deployments that happen to contain ``gateway`` as a substring
        # (e.g. a hypothetical ``litellm-gateway``).
        if name != "gateway" and not name.startswith("gateway-"):
            continue
        vols = _deployment_volumes(dep)
        worktrees_vol = next((v for v in vols if (v or {}).get("name") == "worktrees"), None)
        # Worktrees survive a pod recreation only if declared AND not an
        # emptyDir. Absence / emptyDir both count as ephemeral, so there is
        # nothing for an empty session store to wrongly orphan — no asymmetry.
        if worktrees_vol is None or "emptyDir" in worktrees_vol:
            continue
        egg_state_vol = next((v for v in vols if (v or {}).get("name") == "egg-state"), None)
        # Session store is persistent iff declared AND not emptyDir.
        egg_state_persistent = egg_state_vol is not None and "emptyDir" not in egg_state_vol
        if egg_state_persistent:
            continue
        if egg_state_vol is None:
            detail = (
                "gateway worktrees survive pod recreation but no "
                "``egg-state`` volume is declared, so /home/egg/.egg-state "
                "falls inside the ``home`` emptyDir and the session store is "
                "ephemeral"
            )
        else:
            detail = (
                "gateway worktrees survive pod recreation but its session "
                "store (egg-state volume, /home/egg/.egg-state) is an "
                "emptyDir and does not"
            )
        _warn(
            warnings,
            rule="session-store-not-persistent",
            severity="error",
            resource=f"Deployment/{name}",
            message=(
                f"{detail}; a gateway pod recreation will boot with an empty "
                "session store and delete live pipeline worktrees during "
                "startup cleanup (#3005)"
            ),
        )

    # Rule 7: orchestrator pipeline-state store must share the repos'
    # persistence lifetime (#3070). The orchestrator's StateStore keeps the
    # ``egg/pipeline-state`` worktrees under
    # /home/egg/.egg-state/pipeline-worktree* (the ``egg-state`` volume).
    # The state branch's *commits* live in each repo's .git on the ``repos``
    # volume, but the worktree's working files hold anything saved since the
    # last commit. When an overlay gives ``repos`` a volume that survives pod
    # recreation while ``egg-state`` is ephemeral (``emptyDir`` or absent —
    # in which case /home/egg/.egg-state falls inside the ``home`` emptyDir),
    # a pod recreation rebuilds each state worktree from the last committed
    # branch tip and silently drops everything newer: in #3070 every
    # in-flight pipeline whose record had no commit yet simply vanished
    # (get_status 404, absent from list_tasks). Like rule 6, the check is
    # expressed against ``emptyDir`` rather than ``hostPath`` so PVC/NFS/CSI
    # backings satisfy it, and it self-gates on ``repos`` being persistent so
    # it stays silent on all-emptyDir base/cloud deploys.
    for dep in deployments:
        name = dep.get("metadata", {}).get("name", "<unknown>")
        if name != "orchestrator" and not name.startswith("orchestrator-"):
            continue
        vols = _deployment_volumes(dep)
        repos_vol = next((v for v in vols if (v or {}).get("name") == "repos"), None)
        if repos_vol is None or "emptyDir" in repos_vol:
            continue
        egg_state_vol = next((v for v in vols if (v or {}).get("name") == "egg-state"), None)
        egg_state_persistent = egg_state_vol is not None and "emptyDir" not in egg_state_vol
        if egg_state_persistent:
            continue
        if egg_state_vol is None:
            detail = (
                "orchestrator repos survive pod recreation but no "
                "``egg-state`` volume is declared, so /home/egg/.egg-state "
                "falls inside the ``home`` emptyDir and the pipeline-state "
                "worktree is ephemeral"
            )
        else:
            detail = (
                "orchestrator repos survive pod recreation but its "
                "pipeline-state store (egg-state volume, "
                "/home/egg/.egg-state) is an emptyDir and does not"
            )
        _warn(
            warnings,
            rule="pipeline-state-store-not-persistent",
            severity="error",
            resource=f"Deployment/{name}",
            message=(
                f"{detail}; an orchestrator pod recreation will rebuild the "
                "state worktree from the last committed branch tip and "
                "silently lose any pipeline state saved since (#3070)"
            ),
        )

    return warnings


def validate_deployment_manifests() -> tuple[Response, int]:
    """Static validation of the committed kustomize overlay."""
    runtime = _pkg._current_runtime()
    if runtime != "kubernetes":
        return _not_available_on_runtime()

    body = request.get_json(silent=True) or {}
    overlay = body.get("overlay_path") or _DEFAULT_OVERLAY

    # Resolve overlay relative to the repo root if a relative path was
    # passed.  The orchestrator container has the repo mounted at
    # /home/egg/repos/egg by default.  The final resolved path MUST
    # stay inside one of the recognised repo roots — otherwise an
    # authenticated caller could probe arbitrary filesystem paths via
    # 200/404 differentiation.
    repo_root_candidates = [
        p
        for p in (
            Path(os.environ.get("EGG_REPO_PATH") or ""),
            Path("/home/egg/repos/egg"),
            Path.cwd(),
        )
        if str(p)
    ]
    overlay_path = Path(overlay)
    if not overlay_path.is_absolute():
        for root in repo_root_candidates:
            if root and (root / overlay).exists():
                overlay_path = root / overlay
                break

    # Guard against path traversal — the resolved overlay must sit
    # under a known repo root.
    try:
        resolved = overlay_path.resolve()
        in_scope = any(
            resolved.is_relative_to(root.resolve()) for root in repo_root_candidates if root
        )
    except OSError, RuntimeError:
        in_scope = False
    if not in_scope:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "overlay_path must resolve under a known repo root",
                }
            ),
            400,
        )

    if not overlay_path.exists():
        return (
            jsonify(
                {
                    "success": False,
                    "message": f"overlay not found: {overlay}",
                }
            ),
            404,
        )

    # Detect k3s so we know whether to apply k3s-specific rules.
    try:
        from kubernetes_client import get_kubernetes_client

        k8s = get_kubernetes_client()
        is_k3s, _hint = _pkg._detect_k3s(k8s)
    except Exception:
        is_k3s = False

    try:
        docs = _pkg._run_kustomize(overlay_path)
    except RuntimeError as exc:
        return (
            jsonify({"success": False, "message": str(exc)}),
            500,
        )

    warnings = _pkg._validate_deployment_docs(docs, is_k3s=is_k3s)
    return (
        jsonify(
            {
                "success": True,
                "data": {
                    "overlay_path": str(overlay_path),
                    "is_k3s": bool(is_k3s),
                    "warnings": warnings,
                },
            }
        ),
        200,
    )
