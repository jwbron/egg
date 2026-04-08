#!/usr/bin/env bash
# Install Calico CNI for NetworkPolicy support on k3s.
# k3s default Flannel does not support NetworkPolicies.
# This script is idempotent - safe to run multiple times.
set -euo pipefail

CALICO_VERSION="${CALICO_VERSION:-v3.27.0}"
CALICO_OPERATOR_URL="https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"
CALICO_CR_URL="https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/custom-resources.yaml"

echo "==> Checking for existing Calico installation..."

if kubectl get pods -n calico-system --no-headers 2>/dev/null | grep -q "calico"; then
    echo "Calico is already installed in calico-system namespace."
    echo "Current Calico pods:"
    kubectl get pods -n calico-system
    echo ""
    echo "To force reinstall, remove Calico first:"
    echo "  kubectl delete -f ${CALICO_CR_URL}"
    echo "  kubectl delete -f ${CALICO_OPERATOR_URL}"
    exit 0
fi

# Also check tigera-operator namespace (operator may be installed but not yet running)
if kubectl get namespace tigera-operator 2>/dev/null | grep -q "tigera-operator"; then
    echo "Tigera operator namespace exists. Checking operator status..."
    kubectl get pods -n tigera-operator 2>/dev/null || true
fi

echo "==> Installing Calico operator (${CALICO_VERSION})..."
kubectl create -f "${CALICO_OPERATOR_URL}" 2>/dev/null || \
    kubectl apply -f "${CALICO_OPERATOR_URL}"

echo "==> Waiting for tigera-operator to be ready..."
kubectl wait --for=condition=Available deployment/tigera-operator \
    -n tigera-operator --timeout=120s 2>/dev/null || {
    echo "Warning: tigera-operator not ready after 120s, continuing anyway..."
}

echo "==> Applying Calico custom resource..."
# For k3s, we need to patch the default CR to use the correct CIDR
# k3s default pod CIDR is 10.42.0.0/16
cat <<'EOF' | kubectl apply -f -
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
      - blockSize: 26
        cidr: 10.42.0.0/16
        encapsulation: VXLANCrossSubnet
        natOutgoing: Enabled
        nodeSelector: all()
---
apiVersion: operator.tigera.io/v1
kind: APIServer
metadata:
  name: default
spec: {}
EOF

echo "==> Waiting for Calico pods to be ready..."
echo "    This may take a few minutes on first install..."

# Wait for calico-system namespace to be created
for i in $(seq 1 30); do
    if kubectl get namespace calico-system 2>/dev/null | grep -q "calico-system"; then
        break
    fi
    echo "    Waiting for calico-system namespace... (${i}/30)"
    sleep 5
done

# Wait for calico-node daemonset to be ready
kubectl wait --for=condition=Ready pods -l k8s-app=calico-node \
    -n calico-system --timeout=300s 2>/dev/null || {
    echo "Warning: calico-node pods not ready after 300s."
    echo "Check status with: kubectl get pods -n calico-system"
    exit 1
}

echo ""
echo "==> Calico installation complete!"
echo ""
echo "Calico pods:"
kubectl get pods -n calico-system
echo ""
echo "NetworkPolicy support is now available."
echo "Verify with: kubectl get networkpolicies -A"
