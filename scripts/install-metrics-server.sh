#!/usr/bin/env bash
#
# install-metrics-server.sh - Deploy egg's hostNetwork metrics-server addon.
#
# `make k3s-setup` disables k3s's bundled metrics-server
# (`--disable=metrics-server`) and runs this script instead. The bundled one
# runs on the pod network and cannot reach the kubelet under our Cilium
# datapath, so it never becomes Ready and its dead v1beta1.metrics.k8s.io
# APIService wedges all namespace deletion. The vendored manifest this script
# applies runs in the host netns and works — see k8s/addons/metrics-server.yaml
# for the full rationale.
#
# Idempotent: safe to run multiple times. `kubectl apply` reconciles the
# manifest and the waits below tolerate an already-Ready deployment.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SCRIPT_DIR/../k8s/addons/metrics-server.yaml"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

error() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2
}

# Prerequisites
if ! command -v kubectl &>/dev/null; then
  error "kubectl is not installed or not in PATH"
  exit 1
fi
if ! kubectl cluster-info &>/dev/null; then
  error "Cannot connect to Kubernetes cluster. Is the cluster running?"
  exit 1
fi
if [ ! -f "$MANIFEST" ]; then
  error "Manifest not found at $MANIFEST"
  exit 1
fi

# Guard against the bundled metrics-server: if k3s was installed WITHOUT
# `--disable=metrics-server`, k3s's own (non-hostNetwork) manifest is
# reconciled from /var/lib/rancher/k3s/server/manifests and will fight this
# one — kubelet-unreachable scrapes, perpetual NotReady, wedged namespace
# deletion. Detect the tell-tale HelmChart/addon and refuse rather than
# deploy into a tug-of-war.
# k3s tracks each bundled manifest as an Addon CR (addons.k3s.cattle.io),
# named after the source file — e.g. metrics-server-deployment,
# metrics-server-service. Match the prefix so this is robust across k3s
# versions. If the CRD is absent (non-k3s cluster), the list is empty and we
# don't false-positive.
if kubectl -n kube-system get addons.k3s.cattle.io -o name 2>/dev/null | grep -q 'metrics-server'; then
  error "k3s's bundled metrics-server addon is present — k3s was installed"
  error "without '--disable=metrics-server'. The bundled (non-hostNetwork)"
  error "manifest cannot reach the kubelet under Cilium and will conflict"
  error "with egg's. Reinstall k3s with --disable=metrics-server:"
  error "    make k3s-teardown && make k3s-setup"
  exit 1
fi

log "Applying egg metrics-server addon from $MANIFEST..."
kubectl apply -f "$MANIFEST"

log "Waiting for metrics-server rollout (timeout: 120s)..."
if ! kubectl -n kube-system rollout status deploy/metrics-server --timeout=120s; then
  error "metrics-server did not become Ready. Recent scrape errors:"
  kubectl -n kube-system logs -l k8s-app=metrics-server --tail=10 2>&1 | sed 's/^/    /' >&2
  error "A 'connect: connection refused' to the node IP:10250 means the pod is"
  error "NOT in the host netns — confirm hostNetwork:true survived in the manifest."
  exit 1
fi

# The APIService can lag the pod becoming Ready by a scrape cycle. Poll until
# the aggregation layer reports it Available, so callers (and `kubectl top`)
# don't race a not-yet-registered metrics.k8s.io.
log "Waiting for v1beta1.metrics.k8s.io APIService to become Available..."
deadline=$((SECONDS + 60))
while true; do
  avail=$(kubectl get apiservice v1beta1.metrics.k8s.io \
    -o 'jsonpath={.status.conditions[?(@.type=="Available")].status}' 2>/dev/null || echo "")
  if [ "$avail" = "True" ]; then
    break
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    error "v1beta1.metrics.k8s.io did not become Available within 60s (status: '${avail:-unknown}')."
    error "Inspect with: kubectl get apiservice v1beta1.metrics.k8s.io -o yaml"
    exit 1
  fi
  sleep 3
done

# Final smoke test: a real `kubectl top nodes` proves the scrape path end to
# end (pod Ready + APIService Available is necessary but not sufficient — the
# first scrape must also have landed).
log "Verifying 'kubectl top nodes' returns data..."
deadline=$((SECONDS + 30))
while ! kubectl top nodes &>/dev/null; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    error "'kubectl top nodes' still failing after metrics-server became Available."
    kubectl top nodes 2>&1 | sed 's/^/    /' >&2
    exit 1
  fi
  sleep 3
done

log "metrics-server is Ready; 'kubectl top' is working."
kubectl top nodes 2>&1 | sed 's/^/  /'
