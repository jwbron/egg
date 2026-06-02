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

# `k3s ctr images list` columns: REF TYPE DIGEST SIZE PLATFORMS LABELS.
listing="$(sudo k3s ctr images list 2>/dev/null || true)"

# Safety: if we cannot even see the just-deployed orchestrator image, the
# listing is unreliable (a swallowed sudo password prompt, a containerd
# hiccup). Skip the reap rather than delete against a partial view.
if ! grep -qE "^docker\.io/library/egg-orchestrator:${KEEP_TAG}([[:space:]]|\$)" <<<"$listing"; then
  echo "==> containerd reap: tag '${KEEP_TAG}' not visible in containerd; skipping (nothing reaped)."
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
kept=0
for ref in "${candidates[@]}"; do
  # crictl rmi is CRI-aware: it refuses an image still held by a live container
  # (e.g. a sandbox agent pod from a prior deploy), so an in-use old tag is left
  # alone instead of being yanked out from under the pod.
  if sudo k3s crictl rmi "$ref" >/dev/null 2>&1; then
    echo "   reaped $ref"
    removed=$((removed + 1))
  else
    kept=$((kept + 1))
  fi
done

echo "==> containerd reap: removed ${removed} stale egg image(s); kept ${kept} in-use/undeletable."
