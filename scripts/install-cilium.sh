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

# If Cilium is already installed and ready, skip the install step but
# still run the post-install verification at the end — that way operators
# can use this script as a config audit on a live cluster without having
# to teardown and reinstall to see whether the deployed cilium-config
# matches the conservative datapath flags this script intends.
SKIP_INSTALL=0
if kubectl get daemonset -n kube-system cilium &>/dev/null; then
  DESIRED=$(kubectl get daemonset -n kube-system cilium -o jsonpath='{.status.desiredNumberScheduled}')
  READY=$(kubectl get daemonset -n kube-system cilium -o jsonpath='{.status.numberReady}')

  if [ "$DESIRED" -gt 0 ] && [ "$DESIRED" = "$READY" ]; then
    log "Cilium is already installed and all ${READY}/${DESIRED} nodes are ready."
    log "Skipping install; running post-install verification only."
    log "(To force reinstall, delete the cilium daemonset first.)"
    SKIP_INSTALL=1
  else
    log "Cilium is installed but not fully ready (${READY}/${DESIRED} nodes ready)."
    log "Re-running install to bring it to ready..."
  fi
fi

if [ "$SKIP_INSTALL" -eq 0 ]; then
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

  # Conservative datapath config. kube-proxy replacement, BPF masquerade,
  # and BPF host routing each attach eBPF programs to physical devices; on
  # hosts where the primary NIC is unusual (e.g. a wireless interface) that
  # can blackhole host connectivity entirely. The legacy/iptables datapath
  # keeps Cilium's eBPF on cilium_* interfaces and pod veths only, and still
  # provides full L3/L4 NetworkPolicy enforcement.
  CILIUM_INSTALL_ARGS=(
    --version "$CILIUM_VERSION"
    --set kubeProxyReplacement=false
    --set bpf.masquerade=false
    --set bpf.hostLegacyRouting=true
  )
  log "Running 'cilium install ${CILIUM_INSTALL_ARGS[*]}'..."
  "$CILIUM_BIN" install "${CILIUM_INSTALL_ARGS[@]}"

  log "Waiting for Cilium to be ready (timeout: 300s)..."
  "$CILIUM_BIN" status --wait --wait-duration=5m
fi

# Post-install verification: confirm cilium-config matches the conservative
# datapath flags we passed. cilium-cli's auto-detection prints info messages
# like "Cilium will fully replace all functionalities of kube-proxy" even
# when --set kubeProxyReplacement=false is passed (k3s embeds kube-proxy in
# k3s-agent, so cilium-cli sees no kube-proxy DaemonSet and announces it
# will replace it). The --set flag overrides the helm value during chart
# rendering, but the info-message-vs-real-config mismatch is a property we
# should not rely on silently — assert the deployed values match. Runs on
# both the fresh-install and idempotent-skip paths so operators can audit a
# live cluster's config without re-installing.
log "Verifying cilium-config matches expected conservative datapath..."
verify_failed=0
for kv in \
    'kube-proxy-replacement:false' \
    'enable-bpf-masquerade:false' \
    'enable-host-legacy-routing:true'; do
  key="${kv%%:*}"
  want="${kv##*:}"
  # kubectl jsonpath returns the value directly (empty string if the key
  # is absent), which keeps install-cilium.sh free of a jq runtime dep —
  # no other script in scripts/ uses jq today.
  got=$(kubectl -n kube-system get cm cilium-config -o "jsonpath={.data['$key']}")
  if [ "$got" != "$want" ]; then
    error "cilium-config[$key] = '$got', expected '$want'"
    verify_failed=1
  fi
done
if [ "$verify_failed" -ne 0 ]; then
  error "The cilium-cli may have silently overridden a --set flag during install."
  error "Run 'kubectl -n kube-system get cm cilium-config -o yaml' to inspect."
  exit 1
fi
log "cilium-config matches expected datapath."

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

if [ "$SKIP_INSTALL" -eq 1 ]; then
  log "Cilium verification passed (install skipped, cluster was already ready)."
else
  log "Cilium ${CILIUM_VERSION} installed successfully."
fi
log "Cilium pod status:"
kubectl get pods -n kube-system -l app.kubernetes.io/name=cilium-agent -o wide
