#!/usr/bin/env bash
#
# install-cilium.sh - Install Cilium CNI for Kubernetes NetworkPolicy support
#
# Idempotent: safe to run multiple times. Skips installation if Cilium is
# already present and ready.
#
# Migrating from Calico (#2580 / #2703): an in-place CNI swap on a live
# cluster is not supported. This script refuses to install if Calico
# artifacts are detected. The supported path is:
#     make k3s-teardown && make k3s-setup
#
set -euo pipefail

CILIUM_CLI_VERSION="${CILIUM_CLI_VERSION:-v0.19.2}"
CILIUM_VERSION="${CILIUM_VERSION:-v1.19.4}"

# Pinned SHA256 checksums for the Cilium CLI tarball, per arch.
# Update these when bumping CILIUM_CLI_VERSION.
CILIUM_CLI_SHA256_ARM64="${CILIUM_CLI_SHA256_ARM64:-205ad0eba18a105e4542a5ff41fea74929f8cb4af831d35c0fac591bfe48bbf1}"
CILIUM_CLI_SHA256_AMD64="${CILIUM_CLI_SHA256_AMD64:-aa25cd2542614051573008ed18adb7d697fd78e4b0345db83d1650cdaf4492de}"

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

# Refuse to install on top of Calico. In-place CNI swap is not supported
# (host CNI binaries, conflists, CRDs, tunl0, and per-pod veth pairs all
# persist after deleting the calico-node DaemonSet). The supported
# migration path is a clean k3s teardown + reinstall.
if kubectl get daemonset -n kube-system calico-node &>/dev/null; then
  error "Calico is installed on this cluster."
  error "In-place CNI migration is not supported. To switch to Cilium, run:"
  error "    make k3s-teardown"
  error "    make k3s-setup"
  exit 1
fi
if kubectl get crd -o name 2>/dev/null | grep -q '\.projectcalico\.org$'; then
  error "Calico CRDs are present on this cluster (leftover from a prior install)."
  error "To switch to Cilium, run:"
  error "    make k3s-teardown"
  error "    make k3s-setup"
  exit 1
fi

# If Cilium is already installed and ready, exit early.
if kubectl get daemonset -n kube-system cilium &>/dev/null; then
  DESIRED=$(kubectl get daemonset -n kube-system cilium -o jsonpath='{.status.desiredNumberScheduled}')
  READY=$(kubectl get daemonset -n kube-system cilium -o jsonpath='{.status.numberReady}')

  if [ "$DESIRED" -gt 0 ] && [ "$DESIRED" = "$READY" ]; then
    log "Cilium is already installed and all ${READY}/${DESIRED} nodes are ready."
    log "To force reinstall, delete the cilium daemonset first."
    exit 0
  else
    log "Cilium is installed but not fully ready (${READY}/${DESIRED} nodes ready)."
    log "Re-running install to bring it to ready..."
  fi
fi

# Detect arch
ARCH=$(uname -m)
case "$ARCH" in
  aarch64 | arm64) CLI_ARCH="arm64"; CLI_SHA256="$CILIUM_CLI_SHA256_ARM64" ;;
  x86_64 | amd64)  CLI_ARCH="amd64"; CLI_SHA256="$CILIUM_CLI_SHA256_AMD64" ;;
  *) error "Unsupported architecture: $ARCH"; exit 1 ;;
esac

log "Installing Cilium ${CILIUM_VERSION} via cilium-cli ${CILIUM_CLI_VERSION} (${CLI_ARCH})..."

TMPDIR=$(mktemp -d /tmp/cilium-install.XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT

TARBALL="$TMPDIR/cilium-linux-${CLI_ARCH}.tar.gz"
CLI_URL="https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-${CLI_ARCH}.tar.gz"

log "Downloading cilium-cli from ${CLI_URL}..."
if ! curl -fsSL "$CLI_URL" -o "$TARBALL"; then
  error "Failed to download cilium-cli"
  exit 1
fi

log "Verifying cilium-cli checksum..."
ACTUAL_SHA256=$(sha256sum "$TARBALL" | awk '{print $1}')
if [ "$ACTUAL_SHA256" != "$CLI_SHA256" ]; then
  error "Checksum mismatch for cilium-cli tarball!"
  error "  Expected: $CLI_SHA256"
  error "  Actual:   $ACTUAL_SHA256"
  error "The downloaded tarball may have been tampered with."
  exit 1
fi
log "Checksum verified."

tar -xzf "$TARBALL" -C "$TMPDIR"
CILIUM_BIN="$TMPDIR/cilium"
chmod +x "$CILIUM_BIN"

log "Running 'cilium install --version ${CILIUM_VERSION}'..."
"$CILIUM_BIN" install --version "$CILIUM_VERSION"

log "Waiting for Cilium to be ready (timeout: 300s)..."
"$CILIUM_BIN" status --wait --wait-duration=5m

# Post-install verification: the host CNI config directory should now
# contain Cilium's conflist and nothing from a previous CNI. This catches
# the silent-failure mode where kubelet keeps using a leftover Calico
# conflist because it sorts ahead of Cilium's.
log "Verifying on-host CNI config state..."
CNI_DIR="/etc/cni/net.d"
if [ -d "$CNI_DIR" ]; then
  # On systemd hosts the dir is typically root:root 0755 but readable by
  # everyone; if not we fall back to sudo for the listing only.
  if ! CNI_FILES=$(ls "$CNI_DIR" 2>/dev/null); then
    CNI_FILES=$(sudo ls "$CNI_DIR" 2>/dev/null || echo "")
  fi
  if [ -z "$CNI_FILES" ]; then
    error "No CNI configs found in ${CNI_DIR} after install — kubelet has no CNI to use."
    error "'cilium install' may have returned 0 without the agent dropping its conflist."
    exit 1
  fi
  if echo "$CNI_FILES" | grep -qi calico; then
    error "Stale Calico CNI config still present in ${CNI_DIR}:"
    echo "$CNI_FILES" | sed 's/^/    /' >&2
    error "Run 'make k3s-teardown && make k3s-setup' for a clean install."
    exit 1
  fi
  if ! echo "$CNI_FILES" | grep -qi cilium; then
    error "No Cilium CNI config found in ${CNI_DIR} after install:"
    echo "$CNI_FILES" | sed 's/^/    /' >&2
    exit 1
  fi
fi

log "Cilium ${CILIUM_VERSION} installed successfully."
log "Cilium pod status:"
kubectl get pods -n kube-system -l app.kubernetes.io/name=cilium-agent -o wide
