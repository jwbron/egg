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

# Ensure the portmap CNI plugin binary is available BEFORE running
# 'cilium install'. cni.chainingMode=portmap makes cilium-agent drop a
# conflist that references portmap as soon as the agent pod is up; if
# portmap is missing from /opt/cni/bin at that moment, kubelet retries
# every pending pod (coredns, traefik, local-path-provisioner — none
# use hostPort but all traverse the CNI chain on every ADD) against
# the missing plugin and they fall into sandbox-creation backoff
# (capped at 5 min). Cilium only installs cilium-cni into its binPath
# (default /opt/cni/bin) — not portmap — so we copy it from k3s's
# bundled CNI bin dir, which is already populated by the time this
# script runs. No new network dependency.
#
# sudo test -x (not [ -x ... ]) so a hardened parent-dir mode that
# blocks the invoking user's traverse surfaces as a real permissions
# error instead of a misleading "not found".
log "Verifying portmap CNI plugin binary is available..."
CNI_BIN_DIR="/opt/cni/bin"
if ! sudo test -x "$CNI_BIN_DIR/portmap"; then
  K3S_CNI_BIN_DIR="/var/lib/rancher/k3s/data/current/bin"
  if sudo test -x "$K3S_CNI_BIN_DIR/portmap"; then
    log "  portmap missing from ${CNI_BIN_DIR}; copying from ${K3S_CNI_BIN_DIR}..."
    sudo mkdir -p "$CNI_BIN_DIR"
    sudo cp "$K3S_CNI_BIN_DIR/portmap" "$CNI_BIN_DIR/portmap"
  elif sudo test -e "$K3S_CNI_BIN_DIR/portmap"; then
    error "portmap exists at ${K3S_CNI_BIN_DIR}/portmap but is not executable."
    error "Check filesystem and SELinux permissions on the k3s data dir."
    exit 1
  elif sudo test -e "$CNI_BIN_DIR/portmap"; then
    error "portmap exists at ${CNI_BIN_DIR}/portmap but is not executable,"
    error "and no fallback binary was found at ${K3S_CNI_BIN_DIR}/portmap."
    error "Check file mode (expected 0755) on ${CNI_BIN_DIR}/portmap."
    exit 1
  else
    error "portmap CNI plugin binary not found at ${CNI_BIN_DIR}/portmap or ${K3S_CNI_BIN_DIR}/portmap."
    error "cni.chainingMode=portmap needs portmap at ${CNI_BIN_DIR}/ to handle hostPort —"
    error "without it, pods with hostPort: stay in ContainerCreating indefinitely."
    error "Install the standard CNI plugins (e.g. 'apt-get install -y containernetworking-plugins'"
    error "and copy /usr/lib/cni/portmap to ${CNI_BIN_DIR}/portmap)."
    exit 1
  fi
fi
# Smoke-test the binary so wrong-arch or truncated copies fail here
# instead of as cryptic CNI ADD errors at first pod schedule. Modern
# portmap (e.g. v1.5.1 shipped by k3s and containernetworking-plugins)
# exits 0 with a "CNI portmap plugin vX.Y.Z" banner on stdout when
# invoked with no CNI_* env vars; older versions print a CNI-spec
# error to stderr. The 2>&1 merge means either path matches. A broken
# binary (wrong arch, truncated) fails with ENOEXEC/segfault and
# produces no "CNI" token on either stream.
if ! sudo "$CNI_BIN_DIR/portmap" </dev/null 2>&1 | grep -q CNI; then
  error "portmap binary at ${CNI_BIN_DIR}/portmap failed smoke test."
  error "Likely wrong architecture or a corrupted copy — remove it and re-run."
  exit 1
fi
log "portmap CNI plugin binary OK at ${CNI_BIN_DIR}/portmap."

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
  #
  # cni.chainingMode=portmap chains the standard portmap CNI plugin after
  # Cilium. With kubeProxyReplacement=false Cilium does not implement
  # Kubernetes hostPort itself, and without portmap chaining hostPort
  # mappings (e.g. orchestrator's 9849/9850 in the local overlay) are
  # silently dropped — pods serve fine inside the cluster but the mapped
  # ports never bind on the node, so Claude Code's MCP client at
  # http://localhost:9850/mcp gets connection-refused.
  CILIUM_INSTALL_ARGS=(
    --version "$CILIUM_VERSION"
    --set kubeProxyReplacement=false
    --set bpf.masquerade=false
    --set bpf.hostLegacyRouting=true
    --set cni.chainingMode=portmap
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
chaining_mode_failed=0
for kv in \
    'kube-proxy-replacement:false' \
    'enable-bpf-masquerade:false' \
    'enable-host-legacy-routing:true' \
    'cni-chaining-mode:portmap'; do
  key="${kv%%:*}"
  want="${kv##*:}"
  # kubectl jsonpath returns the value directly (empty string if the key
  # is absent), which keeps install-cilium.sh free of a jq runtime dep —
  # no other script in scripts/ uses jq today.
  got=$(kubectl -n kube-system get cm cilium-config -o "jsonpath={.data['$key']}")
  if [ "$got" != "$want" ]; then
    error "cilium-config[$key] = '$got', expected '$want'"
    verify_failed=1
    if [ "$key" = "cni-chaining-mode" ]; then
      chaining_mode_failed=1
    fi
  fi
done
if [ "$verify_failed" -ne 0 ]; then
  error "The cilium-cli may have silently overridden a --set flag during install."
  if [ "$chaining_mode_failed" -eq 1 ]; then
    error "cni-chaining-mode mismatch suggests this is a Cilium install from before"
    error "#2713. The supported remediation is 'make k3s-teardown && make k3s-setup'"
    error "— the chainingMode value cannot be changed by editing cilium-config on a"
    error "live cluster, as the agent only reads it at startup."
  fi
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

# Post-install: install the pod-egress MASQUERADE rule that cilium-agent
# does NOT install when running in chained-CNI mode.
#
# cni.chainingMode=portmap (which we set above to give us hostPort under
# kubeProxyReplacement=false) puts cilium-agent into a "chained" mode
# where it treats itself as a secondary plugin behind a notional primary
# CNI and defers iptables masquerade to that primary. But here Cilium IS
# the primary (it owns IPAM and the datapath); there is no other primary
# to install the rule. So agent's CILIUM_POST_nat chain stays empty even
# though cilium-config has enable-ipv4-masquerade=true, pod traffic
# leaves the host with its pod-CIDR source IP intact, the internet
# routes responses to an unrouteable address, and any pod that needs
# external egress (gateway -> api.github.com, sandbox agents -> the
# Anthropic API, etc.) silently fails.
#
# Install the equivalent rule directly in POSTROUTING (not
# CILIUM_POST_nat, which the agent flushes on every config sync) so it
# survives agent restarts and config reloads. NOTE: this is a runtime
# iptables rule — it is NOT persisted across host reboots. Re-run this
# script (or `make k3s-setup`) after a reboot, or wire the rule into
# `netfilter-persistent`/`iptables-restore` at the system level.
#
# Idempotent — re-running the script is a no-op if the rule is already
# present.
#
# Footgun: Cilium's default cluster-pool is 10.0.0.0/8 (very broad
# RFC1918) since we don't pass --set ipam.operator.clusterPoolIPv4PodCIDRList.
# On hosts whose own primary IP is in 10.0.0.0/8 (corporate VPNs,
# AWS/GCP VPCs with 10.x subnets), the rule "-s 10.0.0.0/8
# ! -d 10.0.0.0/8 -j MASQUERADE" also matches host-originated traffic
# from that IP; MASQUERADE rewrites source to the outbound iface IP
# (usually the same address) so it's functionally a no-op, but a
# narrower pool (e.g. 10.244.0.0/16) would scope the rule strictly to
# pod traffic if exotic routing topologies become a concern.
#
# IPv6 / dual-stack TODO: only cluster-pool-ipv4-cidr is read. If
# dual-stack is enabled in the future (cilium-config gets
# cluster-pool-ipv6-cidr), an equivalent
# `ip6tables -t nat -A POSTROUTING -s <v6-pool> ! -d <v6-pool> -j MASQUERADE`
# is required or v6 pod egress will silently fail the same way v4 did.
#
# iptables backend skew: this rule lands in whichever backend
# /usr/sbin/iptables points to. Modern Ubuntu and stock k3s both default
# to iptables-nft, so this matches what cilium-agent (and k3s-agent's
# embedded kube-proxy) use. Surface the active backend in the log so a
# rare iptables-legacy host shows up at install time, not as silent
# packet loss later.
log "Installing pod-egress MASQUERADE rule (compensates for chained-CNI mode)..."
update-alternatives --display iptables 2>/dev/null | head -3 | sed 's/^/  iptables-alt: /' || true
# Bracket-notation jsonpath for the hyphenated key — older kubectl
# parsed dotted hyphens as subtraction. `|| true` keeps the assignment
# alive when the key is absent (renamed by a future Cilium release, or
# operator on a non-cluster-pool IPAM mode like ipam.mode=kubernetes/eni);
# without it, `set -euo pipefail` + grep's exit 1 would short-circuit
# the script and the explicit empty-check below would never fire.
POD_POOL_CIDR=$(kubectl -n kube-system get cm cilium-config -o "jsonpath={.data['cluster-pool-ipv4-cidr']}" 2>/dev/null \
  | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}' | head -1 || true)
if [ -z "$POD_POOL_CIDR" ]; then
  error "Could not read cluster-pool-ipv4-cidr from cilium-config — cannot install pod-egress MASQUERADE rule."
  error "Likely causes: non-cluster-pool IPAM mode (ipam.mode=kubernetes/eni), or the key was"
  error "renamed in a newer Cilium release. Inspect with:"
  error "  kubectl -n kube-system get cm cilium-config -o yaml | grep -i cidr"
  error "Pod-to-external traffic (gateway -> GitHub, sandbox agents -> APIs) will fail without the rule."
  exit 1
fi
MASQ_COMMENT="egg: cilium pod egress (chained-mode masquerade compensation)"
if sudo iptables -t nat -C POSTROUTING -s "$POD_POOL_CIDR" ! -d "$POD_POOL_CIDR" -m comment --comment "$MASQ_COMMENT" -j MASQUERADE 2>/dev/null; then
  log "  MASQUERADE rule already present for ${POD_POOL_CIDR}; nothing to do."
else
  sudo iptables -t nat -A POSTROUTING -s "$POD_POOL_CIDR" ! -d "$POD_POOL_CIDR" -m comment --comment "$MASQ_COMMENT" -j MASQUERADE
  log "  Installed MASQUERADE rule for ${POD_POOL_CIDR}."
fi
# Verify — same -C check, just sanity, fail loud if iptables silently rejected the add.
if ! sudo iptables -t nat -C POSTROUTING -s "$POD_POOL_CIDR" ! -d "$POD_POOL_CIDR" -m comment --comment "$MASQ_COMMENT" -j MASQUERADE 2>/dev/null; then
  error "Pod-egress MASQUERADE rule failed to install in POSTROUTING."
  error "Pod-to-external traffic (gateway -> GitHub, sandbox agents -> APIs) will fail."
  exit 1
fi
log "Pod-egress MASQUERADE verified."

if [ "$SKIP_INSTALL" -eq 1 ]; then
  log "Cilium verification passed (install skipped, cluster was already ready)."
else
  log "Cilium ${CILIUM_VERSION} installed successfully."
fi
log "Cilium pod status:"
kubectl get pods -n kube-system -l app.kubernetes.io/name=cilium-agent -o wide
