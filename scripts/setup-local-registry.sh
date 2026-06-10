#!/usr/bin/env bash
#
# setup-local-registry.sh - One-time host setup for the local image registry
# the fast `make redeploy` flow publishes through (issue #2999).
#
# Why a registry at all: `docker save | k3s ctr images import` always
# re-serializes the FULL image — ~10 GB for egg-sandbox — even when one
# 70 MB source layer changed, and that churn is what fragments btrfs into
# DiskPressure on the local dev host. `docker push` + containerd pull are
# both layer-aware: only changed layers ever move. This script provides the
# two pieces that flow needs:
#
#   1. A `registry:2` container (name: egg-registry) on 127.0.0.1:<port>,
#      restart=always, blobs in the `egg-registry-data` docker volume,
#      DELETE enabled so reap-stale-egg-images.sh can prune old tags.
#   2. /etc/rancher/k3s/registries.yaml telling k3s's containerd to reach
#      the registry over plain HTTP (containerd defaults to HTTPS even for
#      localhost). k3s only reads this file at startup, so writing it
#      requires one `systemctl restart k3s` — that restarts the k3s
#      service, not the running pods.
#
# Idempotent: re-running repairs a stopped container and skips pieces that
# are already in place. The registry binds 127.0.0.1 only — nothing
# off-host can reach it.
#
# Step 2 needs sudo. Run attended (`make registry-setup`) so it can prompt;
# without sudo it prints the exact commands to run and exits non-zero.
#
set -euo pipefail

REGISTRY="${1:-localhost:5000}"
PORT="${REGISTRY##*:}"
HOST="${REGISTRY%%:*}"
CONTAINER_NAME="egg-registry"
VOLUME_NAME="egg-registry-data"
REGISTRIES_YAML="/etc/rancher/k3s/registries.yaml"

if [ "$HOST" != "localhost" ] && [ "$HOST" != "127.0.0.1" ]; then
  echo "ERROR: this script only sets up a loopback registry (got host '$HOST')." >&2
  echo "       A non-local registry needs TLS/auth decisions it can't make for you." >&2
  exit 1
fi

# --- 1. Registry container -------------------------------------------------

existing="$(docker ps -a --filter "name=^${CONTAINER_NAME}$" --format '{{.Status}}')"
if [ -n "$existing" ]; then
  case "$existing" in
    Up*)
      echo "==> Registry container '${CONTAINER_NAME}' already running."
      ;;
    *)
      echo "==> Starting existing registry container '${CONTAINER_NAME}'..."
      docker start "$CONTAINER_NAME" >/dev/null
      ;;
  esac
else
  echo "==> Creating registry container '${CONTAINER_NAME}' on 127.0.0.1:${PORT}..."
  # noqa: EGG100 - loopback-only image registry backing the fast redeploy publish path (issue #2999)
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart=always \
    -p "127.0.0.1:${PORT}:5000" \
    -v "${VOLUME_NAME}:/var/lib/registry" \
    -e REGISTRY_STORAGE_DELETE_ENABLED=true \
    registry:2 >/dev/null
fi

echo "==> Waiting for the registry to answer..."
for _ in $(seq 1 30); do
  if curl -fsS "http://${REGISTRY}/v2/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl -fsS "http://${REGISTRY}/v2/" >/dev/null 2>&1; then
  echo "ERROR: registry container is up but http://${REGISTRY}/v2/ does not answer." >&2
  echo "       Check: docker logs ${CONTAINER_NAME}" >&2
  exit 1
fi
echo "    Registry answering at http://${REGISTRY}/v2/"

# --- 2. k3s registries.yaml ------------------------------------------------

if [ ! -d /etc/rancher/k3s ]; then
  echo "==> /etc/rancher/k3s not found — k3s is not installed on this host."
  echo "    Skipping registries.yaml; re-run 'make registry-setup' after 'make k3s-setup'."
  exit 0
fi

# The mirror entry maps the image-name registry host (what pod specs and
# `docker push` use) to a plain-HTTP endpoint. Without it containerd tries
# HTTPS against the cleartext registry and every pull fails with
# "http: server gave HTTP response to HTTPS client".
wanted_yaml="$(
  cat <<EOF
mirrors:
  "${REGISTRY}":
    endpoint:
      - "http://127.0.0.1:${PORT}"
EOF
)"

# Plain (non-sudo) reads: this script writes the file 0644 below, and k3s
# leaves /etc/rancher/k3s traversable, so reading needs no privilege.
if [ -f "$REGISTRIES_YAML" ]; then
  if grep -qs "\"${REGISTRY}\":" "$REGISTRIES_YAML"; then
    echo "==> ${REGISTRIES_YAML} already maps ${REGISTRY}; leaving it alone."
    exit 0
  fi
  echo "ERROR: ${REGISTRIES_YAML} exists but has no entry for ${REGISTRY}." >&2
  echo "       Merge this mirror block into it by hand, then 'sudo systemctl restart k3s':" >&2
  echo "" >&2
  echo "$wanted_yaml" >&2
  exit 1
fi

echo "==> Writing ${REGISTRIES_YAML} and restarting k3s to pick it up..."
echo "    (k3s only reads registries.yaml at startup; the restart bounces the"
echo "    k3s service itself, not the running pods)"
if ! printf '%s\n' "$wanted_yaml" | sudo tee "$REGISTRIES_YAML" >/dev/null; then
  echo "ERROR: could not write ${REGISTRIES_YAML} (no sudo?). Run by hand:" >&2
  echo "" >&2
  echo "  sudo tee ${REGISTRIES_YAML} <<'EOF'" >&2
  echo "$wanted_yaml" >&2
  echo "EOF" >&2
  echo "  sudo systemctl restart k3s" >&2
  exit 1
fi
sudo chmod 644 "$REGISTRIES_YAML"
sudo systemctl restart k3s

echo "==> Waiting for the k3s node to come back Ready..."
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
for _ in $(seq 1 60); do
  if kubectl wait --for=condition=Ready node --all --timeout=5s >/dev/null 2>&1; then
    echo "==> Local registry setup complete."
    exit 0
  fi
  sleep 2
done
echo "ERROR: k3s did not report Ready within ~2 minutes of the restart." >&2
echo "       Check: systemctl status k3s / journalctl -u k3s" >&2
exit 1
