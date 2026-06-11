#!/usr/bin/env bash
#
# btrfs-reclaim.sh - Return btrfs over-allocated data chunks to unallocated
# (issue #2999 lever B).
#
# Heavy image churn (docker builds, containerd imports/pulls, tarball
# create/delete) fragments btrfs into many near-empty-but-allocated data
# chunks. statfs counts allocated chunks as used, so kubelet's imagefs
# accounting reads ~86% on a disk that is really ~50% full: image GC fires
# and evicts freshly-published images, DiskPressure sticks and evicts pods —
# and deleting images does NOT help, because chunk allocation only returns
# to the pool via a balance. This script runs that balance.
#
# Usage: btrfs-reclaim.sh [dusage-percent] [mountpoint]
#   dusage-percent (default 50): only data chunks <= this %-full are
#     compacted. Higher reclaims more but moves more data (slower).
#   mountpoint (default /): filesystem to balance.
#
# No-op (exit 0) on non-btrfs filesystems, so callers can invoke it
# unconditionally. Needs sudo (balance is privileged). Safe to run on a live
# system — balance is online — but it is I/O-heavy and can take minutes.
#
set -euo pipefail

DUSAGE="${1:-50}"
MOUNT="${2:-/}"

fstype="$(stat -f --format=%T "$MOUNT" 2>/dev/null || echo unknown)"
if [ "$fstype" != "btrfs" ]; then
  echo "==> ${MOUNT} is ${fstype}, not btrfs; nothing to reclaim."
  exit 0
fi

echo "==> btrfs usage on ${MOUNT} before balance:"
sudo btrfs filesystem usage "$MOUNT" | sed -n '1,10p'

echo "==> Balancing data chunks <=${DUSAGE}% full (online, I/O-heavy, can take minutes)..."
sudo btrfs balance start -dusage="$DUSAGE" "$MOUNT"

echo "==> btrfs usage on ${MOUNT} after balance:"
sudo btrfs filesystem usage "$MOUNT" | sed -n '1,10p'
