#!/usr/bin/env bash
#
# install-calico.sh - Install Calico CNI for Kubernetes NetworkPolicy support
#
# Idempotent: safe to run multiple times. Skips installation if Calico is
# already present and running.
#
set -euo pipefail

CALICO_VERSION="${CALICO_VERSION:-v3.31.5}"
CALICO_MANIFEST_URL="https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"

# SHA256 checksum for the known-good v3.31.5 manifest.
# Update this hash when bumping CALICO_VERSION.
CALICO_MANIFEST_SHA256="${CALICO_MANIFEST_SHA256:-}"
CALICO_V3_31_5_SHA256="d45842abe9f95afb4d346278eafb2e454dacdfb502d48cf1d5cede71a9046997"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

error() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2
}

# Check prerequisites
if ! command -v kubectl &>/dev/null; then
  error "kubectl is not installed or not in PATH"
  exit 1
fi

if ! kubectl cluster-info &>/dev/null; then
  error "Cannot connect to Kubernetes cluster. Is the cluster running?"
  exit 1
fi

# Check if Calico is already installed and running
if kubectl get daemonset -n kube-system calico-node &>/dev/null; then
  DESIRED=$(kubectl get daemonset -n kube-system calico-node -o jsonpath='{.status.desiredNumberScheduled}')
  READY=$(kubectl get daemonset -n kube-system calico-node -o jsonpath='{.status.numberReady}')

  if [ "$DESIRED" -gt 0 ] && [ "$DESIRED" = "$READY" ]; then
    log "Calico is already installed and all ${READY}/${DESIRED} nodes are ready."
    log "To force reinstall, delete the calico-node daemonset first."
    exit 0
  else
    log "Calico is installed but not fully ready (${READY}/${DESIRED} nodes ready)."
    log "Re-applying manifests and waiting for readiness..."
  fi
fi

log "Installing Calico ${CALICO_VERSION}..."

# Download and apply Calico manifests
TMPFILE=$(mktemp /tmp/calico-manifest.XXXXXX.yaml)
trap 'rm -f "$TMPFILE"' EXIT

log "Downloading Calico manifests from ${CALICO_MANIFEST_URL}..."
if ! curl -fsSL "$CALICO_MANIFEST_URL" -o "$TMPFILE"; then
  error "Failed to download Calico manifests"
  exit 1
fi

# Verify checksum when using the default version and no override is set
if [ -z "$CALICO_MANIFEST_SHA256" ] && [ "$CALICO_VERSION" = "v3.31.5" ]; then
  CALICO_MANIFEST_SHA256="$CALICO_V3_31_5_SHA256"
fi

if [ -n "$CALICO_MANIFEST_SHA256" ]; then
  log "Verifying manifest checksum..."
  ACTUAL_SHA256=$(sha256sum "$TMPFILE" | awk '{print $1}')
  if [ "$ACTUAL_SHA256" != "$CALICO_MANIFEST_SHA256" ]; then
    error "Checksum mismatch for Calico manifest!"
    error "  Expected: $CALICO_MANIFEST_SHA256"
    error "  Actual:   $ACTUAL_SHA256"
    error "The downloaded manifest may have been tampered with."
    exit 1
  fi
  log "Checksum verified."
else
  log "WARNING: No checksum available for Calico ${CALICO_VERSION}. Skipping verification."
  log "Set CALICO_MANIFEST_SHA256 to enable checksum verification for custom versions."
fi

log "Applying Calico manifests..."
if ! kubectl apply -f "$TMPFILE"; then
  error "Failed to apply Calico manifests"
  exit 1
fi

# Wait for calico-node pods to be ready
log "Waiting for calico-node daemonset to be ready (timeout: 300s)..."
if ! kubectl rollout status daemonset/calico-node -n kube-system --timeout=300s; then
  error "calico-node daemonset did not become ready within 300 seconds"
  log "Current status:"
  kubectl get pods -n kube-system -l k8s-app=calico-node -o wide
  exit 1
fi

# Verify calico-kube-controllers deployment
log "Waiting for calico-kube-controllers to be ready (timeout: 120s)..."
if ! kubectl rollout status deployment/calico-kube-controllers -n kube-system --timeout=120s; then
  error "calico-kube-controllers did not become ready within 120 seconds"
  log "Current status:"
  kubectl get pods -n kube-system -l k8s-app=calico-kube-controllers -o wide
  exit 1
fi

log "Calico ${CALICO_VERSION} installed successfully."
log "Calico node status:"
kubectl get pods -n kube-system -l k8s-app=calico-node -o wide
log "Calico controller status:"
kubectl get pods -n kube-system -l k8s-app=calico-kube-controllers -o wide
