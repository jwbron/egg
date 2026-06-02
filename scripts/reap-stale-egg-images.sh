#!/usr/bin/env bash
#
# reap-stale-egg-images.sh - After a successful deploy, drop the egg-*:<tag>
# images in k3s's containerd that are NOT the just-deployed tag (and NOT the
# floating :latest, which shares content with it).
#
# Without this, containerd keeps a full image set -- including the ~12 GB
# egg-sandbox -- for every `git describe` tag ever imported, because
# `make redeploy` only ever adds the new tag and never removes the old one.
# That bloat drives the root filesystem over kubelet's
# imageGCHighThresholdPercent (~85%), and kubelet image GC then evicts the
# freshly-imported egg images mid-`make redeploy`: they are unreferenced until
# `deploy` repoints the pods at the new tag, so they are prime GC fodder. The
# gateway/orchestrator images -- imported first, so already older than
# imageMinimumGCAge (~2 min) by the time the long sandbox/litellm imports
# finish -- are the ones that vanish, while sandbox/litellm (still inside the
# GC-immunity window) survive. check-egg-images-present.sh then aborts the
# deploy. Reaping here bounds containerd so the next redeploy's import spike
# stays under the GC threshold.
#
# NOTE: this is NOT `crictl rmi --prune`. Prune removes every image no running
# container references -- but the egg-sandbox image is referenced only by
# agent pods the orchestrator spawns on demand, so between runs no pod holds
# it and prune would delete the *current* sandbox image. This reap explicitly
# keeps the just-deployed tag for all four images.
#
# Best-effort: a reap failure never fails the deploy (the Makefile guards the
# call with `|| true`, and each removal below is individually tolerant).
# Requires sudo because the containerd socket is root-only, same as k3s-import.
#
set -euo pipefail

: "${1:?usage: $0 <egg-image-tag-to-keep>}"
KEEP_TAG="$1"

# Keep in sync with check-egg-images-present.sh and the k3s-import image list.
IMAGES=(egg-gateway egg-orchestrator egg-sandbox egg-litellm)

# `k3s ctr images list` columns: REF TYPE DIGEST SIZE PLATFORMS LABELS.
listing="$(sudo k3s ctr images list 2>/dev/null || true)"

# Safety: require ALL four just-deployed egg-*:KEEP_TAG refs to be visible
# before we reap anything. If even one is missing -- a swallowed sudo prompt,
# a containerd hiccup, an out-of-band crictl rmi between pre-flight and reap,
# kubelet GC eating an unreferenced image -- the awk loop would not record
# that image's KEEP_TAG digest, so every prior egg-<that-image>:* tag would
# look stale and get reaped. That is the worst-case outcome of this whole
# script: the next agent-pod spawn (sandbox especially) cannot find an image
# to run. Skip the reap entirely instead.
missing_keep=()
for img in "${IMAGES[@]}"; do
  if ! grep -qE "^docker\.io/library/${img}:${KEEP_TAG}([[:space:]]|\$)" <<<"$listing"; then
    missing_keep+=("${img}:${KEEP_TAG}")
  fi
done
if [ "${#missing_keep[@]}" -gt 0 ]; then
  echo "==> containerd reap: not all egg-*:${KEEP_TAG} refs visible (${missing_keep[*]}); skipping (nothing reaped)."
  exit 0
fi

# Reap candidates: every egg-* ref whose tag is neither KEEP_TAG nor latest AND
# whose manifest digest differs from every kept ref's digest. The digest guard
# matters because a commit that does not change an image's build inputs yields a
# tag whose content is byte-identical to the current one -- same digest, same
# image ID. crictl rmi removes by image ID (all of that ID's tags), so removing
# such a stale tag by name would take the current image with it -- and the
# sandbox image has no running pod to make crictl refuse the removal. Skipping
# by digest leaves those harmless duplicate tags in place; they cost no disk.
mapfile -t candidates < <(awk -v keep="$KEEP_TAG" '
  $1 ~ /^docker\.io\/library\/egg-(gateway|orchestrator|sandbox|litellm):/ {
    ref = $1; dig = $3
    tag = ref; sub(/.*:/, "", tag)
    if (tag == keep || tag == "latest") { keepdig[dig] = 1; next }
    cand_ref[NR] = ref; cand_dig[NR] = dig
  }
  END {
    for (nr in cand_ref) if (!(cand_dig[nr] in keepdig)) print cand_ref[nr]
  }
' <<<"$listing")

if [ "${#candidates[@]}" -eq 0 ]; then
  echo "==> containerd reap: no stale egg images beyond tag '${KEEP_TAG}'/latest."
  exit 0
fi

removed=0
rmi_failed=0
for ref in "${candidates[@]}"; do
  # crictl rmi is best-effort here. The CRI RemoveImage RPC's behavior on an
  # in-use image is implementation-defined -- containerd's CRI plugin generally
  # allows the removal (the snapshot stays mounted under the running container
  # until exit), and there have been "rmi removed image out from under running
  # container" reports historically. We do not rely on a refusal: a non-zero
  # exit here just means "this ref still exists; we did not remove it," and
  # since the digest guard above already excluded any ref that shares an image
  # ID with a kept ref, leaving it alone is safe either way. Do NOT relax the
  # digest guard or the `|| true` on the Makefile call on the assumption that
  # crictl will refuse in-use removals -- it may not.
  err="$(sudo k3s crictl rmi "$ref" 2>&1 >/dev/null)" && rc=0 || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "   reaped $ref"
    removed=$((removed + 1))
  else
    echo "   rmi failed for $ref: ${err:-(no stderr)}" >&2
    rmi_failed=$((rmi_failed + 1))
  fi
done

echo "==> containerd reap: removed ${removed} stale egg image(s); ${rmi_failed} rmi call(s) failed."
