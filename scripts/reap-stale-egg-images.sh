#!/usr/bin/env bash
#
# reap-stale-egg-images.sh - After a successful deploy, drop the egg images
# that are NOT the just-deployed tag (and NOT the floating :latest, which
# shares content with it).
#
# Without this, containerd keeps a full image set -- including the ~10 GB
# egg-sandbox -- for every `git describe` tag ever deployed, because
# `make redeploy` only ever adds the new tag and never removes the old one.
# That bloat drives the root filesystem over kubelet's
# imageGCHighThresholdPercent (~85%), and on btrfs the resulting churn
# over-allocates data chunks into sticky DiskPressure (issue #2999).
#
# Four scopes (issue #2999):
#   - containerd (always): remove stale egg refs via crictl rmi. An image's
#     authoritative ref form depends on its publish path: registry-subset
#     images (args 3+, all of them by default) are authoritative as
#     <registry>/<image>:<tag>, while save+import images are authoritative as
#     docker.io/library/<image>:<tag>. Refs in the non-authoritative form for
#     their image -- e.g. bare leftovers from before the registry flow -- are
#     stale by definition (the digest guard below still protects
#     shared-content cases). With no registry every image is bare -- the
#     pre-registry behavior, unchanged.
#   - the docker daemon store + BuildKit cache (always, lever C): untag stale
#     egg tags and cap the build cache, so the build side stops accumulating
#     a ~10 GB image per commit ever built.
#   - btrfs chunk over-allocation (btrfs hosts, lever B): warn when
#     unallocated space runs low, auto-balance when critically low.
#   - the registry itself (registry mode only): delete manifests for stale
#     tags and run the registry's garbage-collect so blob disk is actually
#     reclaimed. Layers are content-addressed, so a redeploy only adds the
#     changed layers (~tens of MB) -- but without this reap those still
#     accumulate without bound.
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

usage="usage: $0 <egg-image-tag-to-keep> [registry-host:port] [registry-image]..."
: "${1:?$usage}"
KEEP_TAG="$1"
REGISTRY="${2:-}"
shift
[ "$#" -gt 0 ] && shift

# The full egg image set. This list is the single source of truth WITHIN this
# script (the safety-gate loop and the awk match-patterns are driven from it).
# Keep in sync ACROSS scripts with EGG_ALL_IMAGES in the Makefile and
# check-egg-images-present.sh.
IMAGES=(egg-gateway egg-orchestrator egg-sandbox egg-litellm)

# Args 3+ name the registry-subset images (EGG_REGISTRY_IMAGES — all egg
# images by default). An image's AUTHORITATIVE containerd ref is
# <registry>/<image>:<tag> when it is in the subset and
# docker.io/library/<image>:<tag> when it is not (save+import path). With no
# registry the subset is forced empty — every image is bare, the
# pre-registry behavior, unchanged.
REGISTRY_SUBSET=()
if [ -n "$REGISTRY" ]; then
  REGISTRY_SUBSET=("$@")
fi
is_registry_image() {
  local img="$1" r
  for r in "${REGISTRY_SUBSET[@]}"; do
    [ "$r" = "$img" ] && return 0
  done
  return 1
}

# Escape regex metacharacters before interpolating into grep -E / awk EREs.
# Real `git describe` outputs ("v1.2.3-4-gabc123") only contain `.` as a regex
# metacharacter -- but escaping everything costs nothing and keeps a future
# tag or registry scheme from quietly breaking the gate. `/` is deliberately
# NOT in the class: it is not an ERE metacharacter, and a `\/` inside a
# string-built regex makes gawk warn "escape sequence not a known regexp
# operator" every time these land in the awk programs below.
escape_re() { printf '%s' "$1" | sed -e 's/[][\\.*^$+?(){}|]/\\&/g'; }
KEEP_TAG_RE="$(escape_re "$KEEP_TAG")"

LEGACY_PREFIX="docker.io/library/"
LEGACY_PREFIX_RE="$(escape_re "$LEGACY_PREFIX")"
REGISTRY_PREFIX_RE=""
[ -n "$REGISTRY" ] && REGISTRY_PREFIX_RE="$(escape_re "${REGISTRY}/")"

# Image-name alternations split by authority, plus the combined candidate
# pattern. '^$' is the deliberate never-matches placeholder for an empty
# side (refs are never empty strings).
reg_img_alt=""
bare_img_alt=""
for img in "${IMAGES[@]}"; do
  if is_registry_image "$img"; then
    reg_img_alt="${reg_img_alt:+${reg_img_alt}|}${img}"
  else
    bare_img_alt="${bare_img_alt:+${bare_img_alt}|}${img}"
  fi
done
IMAGE_RE="$(IFS='|'; echo "${IMAGES[*]}")"
if [ -n "$REGISTRY" ]; then
  PREFIX_ALT_RE="${REGISTRY_PREFIX_RE}|${LEGACY_PREFIX_RE}"
else
  PREFIX_ALT_RE="$LEGACY_PREFIX_RE"
fi
AUTH_REG_RE='^$'
[ -n "$reg_img_alt" ] && AUTH_REG_RE="^${REGISTRY_PREFIX_RE}(${reg_img_alt}):"
AUTH_BARE_RE='^$'
[ -n "$bare_img_alt" ] && AUTH_BARE_RE="^${LEGACY_PREFIX_RE}(${bare_img_alt}):"

# `k3s ctr images list` columns: REF TYPE DIGEST SIZE PLATFORMS LABELS.
listing="$(sudo k3s ctr images list 2>/dev/null || true)"

# Safety: require ALL just-deployed egg-*:KEEP_TAG refs to be visible before we
# reap anything. If even one is missing -- a swallowed sudo prompt, a
# containerd hiccup, an out-of-band crictl rmi between pre-flight and reap,
# kubelet GC eating an unreferenced image -- the awk loop would not record
# that image's KEEP_TAG digest, so every prior egg-<that-image>:* tag would
# look stale and get reaped. That is the worst-case outcome of this whole
# script: the next agent-pod spawn (sandbox especially) cannot find an image
# to run. Skip the reap entirely instead.
missing_keep=()
for img in "${IMAGES[@]}"; do
  if is_registry_image "$img"; then
    expect_prefix_re="$REGISTRY_PREFIX_RE"
  else
    expect_prefix_re="$LEGACY_PREFIX_RE"
  fi
  if ! grep -qE "^${expect_prefix_re}${img}:${KEEP_TAG_RE}([[:space:]]|\$)" <<<"$listing"; then
    missing_keep+=("${img}:${KEEP_TAG}")
  fi
done
if [ "${#missing_keep[@]}" -gt 0 ]; then
  echo "==> containerd reap: not all egg-*:${KEEP_TAG} refs visible (${missing_keep[*]}); skipping (nothing reaped)."
  exit 0
fi

# Reap candidates: every egg ref (authoritative or legacy prefix) whose tag is
# neither KEEP_TAG nor latest *on the authoritative prefix* AND whose manifest
# digest differs from every kept ref's digest. The digest guard matters
# because a commit that does not change an image's build inputs yields a tag
# whose content is byte-identical to the current one -- same digest, same
# image ID. crictl rmi removes by image ID (all of that ID's tags), so removing
# such a stale tag by name would take the current image with it -- and the
# sandbox image has no running pod to make crictl refuse the removal. Skipping
# by digest leaves those harmless duplicate tags in place; they cost no disk.
#
# The awk match patterns are built from the alternations above so a fifth
# image added to IMAGES flows in automatically. A ref is KEPT when it is the
# authoritative form for its image (registry-qualified for the registry
# subset, bare for the rest) AND carries KEEP_TAG/latest; everything else
# matching an egg image name under either prefix is a candidate. The regex
# fragments ride in via the environment, NOT -v: gawk escape-processes -v
# values, so the `\.` in "docker\.io/library/" would both warn and lose its
# backslash. ENVIRON[] is read verbatim.
mapfile -t candidates < <(KEEP_TAG="$KEEP_TAG" IMAGE_RE="$IMAGE_RE" \
  PREFIX_ALT_RE="$PREFIX_ALT_RE" AUTH_REG_RE="$AUTH_REG_RE" \
  AUTH_BARE_RE="$AUTH_BARE_RE" awk '
  BEGIN {
    keep = ENVIRON["KEEP_TAG"]
    match_re = "^(" ENVIRON["PREFIX_ALT_RE"] ")(" ENVIRON["IMAGE_RE"] "):"
    auth_reg_re = ENVIRON["AUTH_REG_RE"]
    auth_bare_re = ENVIRON["AUTH_BARE_RE"]
  }
  $1 ~ match_re {
    ref = $1; dig = $3
    tag = ref; sub(/.*:/, "", tag)
    if ((ref ~ auth_reg_re || ref ~ auth_bare_re) && (tag == keep || tag == "latest")) {
      keepdig[dig] = 1; next
    }
    cand_ref[NR] = ref; cand_dig[NR] = dig
  }
  END {
    for (nr in cand_ref) if (!(cand_dig[nr] in keepdig)) print cand_ref[nr]
  }
' <<<"$listing")

if [ "${#candidates[@]}" -eq 0 ]; then
  echo "==> containerd reap: no stale egg images beyond tag '${KEEP_TAG}'/latest."
else
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
fi

# --- docker daemon store reap (issue #2999 lever C) --------------------------

# `make build` mints a fresh :<git-describe> tag set per commit and re-points
# :latest, so without a reap the docker daemon's store (a SEPARATE store from
# containerd's) accumulates one ~10 GB sandbox image per commit ever built.
# Untag every egg ref — bare and registry-qualified — whose tag is neither
# KEEP_TAG nor latest. `docker rmi` without -f only untags while other tags
# reference the same image, so layers shared with the kept tags survive; data
# is freed only when the last referencing tag drops.
if [ -n "$REGISTRY" ]; then
  REGISTRY_RE="$(escape_re "$REGISTRY")"
  DOCKER_REF_RE="^(${REGISTRY_RE}/)?(${IMAGE_RE}):"
else
  DOCKER_REF_RE="^(${IMAGE_RE}):"
fi
# Regex via ENVIRON, not -v, for the same escape-processing reason as the
# containerd awk above.
mapfile -t docker_stale < <(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null |
  KEEP_TAG="$KEEP_TAG" DOCKER_REF_RE="$DOCKER_REF_RE" awk '
    $0 ~ ENVIRON["DOCKER_REF_RE"] {
      tag = $0; sub(/.*:/, "", tag)
      if (tag != ENVIRON["KEEP_TAG"] && tag != "latest" && tag != "<none>") print
    }')
if [ "${#docker_stale[@]}" -eq 0 ]; then
  echo "==> docker store reap: no stale egg tags beyond '${KEEP_TAG}'/latest."
else
  docker_removed=0
  for ref in "${docker_stale[@]}"; do
    if docker rmi "$ref" >/dev/null 2>&1; then
      docker_removed=$((docker_removed + 1))
    fi
  done
  echo "==> docker store reap: untagged ${docker_removed}/${#docker_stale[@]} stale egg tag(s)."
fi

# Cap the BuildKit build cache (the other unbounded docker-side store). The
# default is generous on purpose: the sandbox stage-1 cache (repo deps,
# multi-GB) is expensive to rebuild, and prune is LRU — a too-small cap would
# silently turn every redeploy into a cold dependency build. Override with
# EGG_BUILDKIT_CACHE_CAP.
docker builder prune -f --keep-storage="${EGG_BUILDKIT_CACHE_CAP:-40GB}" >/dev/null 2>&1 || true

# --- btrfs chunk-reclaim check (issue #2999 lever B) --------------------------

# On btrfs, the churn above over-allocates data chunks; statfs counts
# allocated-but-empty chunks as used, kubelet's imagefs accounting crosses its
# ~85% GC threshold on a half-empty disk, and DiskPressure wedges the node.
# Deleting images does NOT return chunks — only a balance does. Auto-balance
# only when unallocated space is critically low (the next redeploy would
# likely wedge); otherwise just point at `make btrfs-reclaim`.
if [ "$(stat -f --format=%T / 2>/dev/null)" = "btrfs" ]; then
  unalloc_bytes="$(sudo btrfs filesystem usage -b / 2>/dev/null |
    awk '/Device unallocated:/ { print $3 }')"
  if [ -n "${unalloc_bytes:-}" ]; then
    unalloc_gib=$((unalloc_bytes / 1073741824))
    if [ "$unalloc_gib" -lt 4 ]; then
      echo "==> btrfs: only ${unalloc_gib} GiB unallocated — reclaiming chunks now (balance, can take minutes)..."
      scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
      "${scripts_dir}/btrfs-reclaim.sh" 50 / || echo "==> btrfs reclaim failed (non-fatal)." >&2
    elif [ "$unalloc_gib" -lt 16 ]; then
      echo "==> btrfs: ${unalloc_gib} GiB unallocated; run 'make btrfs-reclaim' soon or DiskPressure will wedge redeploys (issue #2999)."
    fi
  fi
fi

# --- registry-side reap (registry mode only) --------------------------------

[ -n "$REGISTRY" ] && [ "${#REGISTRY_SUBSET[@]}" -gt 0 ] || exit 0

API="http://${REGISTRY}/v2"
if ! curl -fsS "${API}/" >/dev/null 2>&1; then
  echo "==> registry reap: registry at ${API} not answering; skipping."
  exit 0
fi

# The Accept header must enumerate the modern manifest types or the registry
# answers 404 for images pushed by current docker/buildkit.
ACCEPT='application/vnd.docker.distribution.manifest.v2+json'
ACCEPT="${ACCEPT}, application/vnd.docker.distribution.manifest.list.v2+json"
ACCEPT="${ACCEPT}, application/vnd.oci.image.manifest.v1+json"
ACCEPT="${ACCEPT}, application/vnd.oci.image.index.v1+json"

# Docker-Content-Digest response header for <repo>:<tag>, empty if absent.
# The trailing `|| true` is load-bearing: a missing tag makes curl -f exit 22,
# and under set -e + pipefail a failing $(manifest_digest ...) inside an
# assignment would abort the whole reap mid-flight.
manifest_digest() {
  curl -fsSI -H "Accept: ${ACCEPT}" "${API}/$1/manifests/$2" 2>/dev/null |
    awk 'tolower($1) == "docker-content-digest:" { gsub("\r", "", $2); print $2 }' || true
}

reg_removed=0
for img in "${REGISTRY_SUBSET[@]}"; do
  tags_json="$(curl -fsS "${API}/${img}/tags/list" 2>/dev/null)" || continue
  mapfile -t tags < <(printf '%s' "$tags_json" |
    python3 -c 'import json, sys
for t in json.load(sys.stdin).get("tags") or []:
    print(t)' 2>/dev/null || true)

  # Digests behind the kept tags. Deleting a manifest by digest unlinks EVERY
  # tag that points at it, so any digest shared with a kept tag must survive.
  keep_digs=" $(manifest_digest "$img" "$KEEP_TAG") $(manifest_digest "$img" latest) "

  for tag in "${tags[@]}"; do
    [ "$tag" = "$KEEP_TAG" ] && continue
    [ "$tag" = "latest" ] && continue
    dig="$(manifest_digest "$img" "$tag")"
    [ -n "$dig" ] || continue
    case "$keep_digs" in *" $dig "*) continue ;; esac
    if curl -fsS -X DELETE "${API}/${img}/manifests/${dig}" >/dev/null 2>&1; then
      echo "   registry: deleted ${img}:${tag} (${dig})"
      reg_removed=$((reg_removed + 1))
    fi
  done
done

# Manifest DELETE only unlinks; the registry's offline GC is what returns
# blob disk. --delete-untagged also collects manifests orphaned by tag
# overwrites (every redeploy re-points :latest, stranding the previous
# manifest untagged-but-stored). Safe here because this runs post-deploy
# when no push is in flight -- do NOT run it concurrently with a build.
if docker exec egg-registry registry garbage-collect --delete-untagged \
  /etc/docker/registry/config.yml >/dev/null 2>&1; then
  echo "==> registry reap: deleted ${reg_removed} stale tag manifest(s); garbage-collect done."
else
  echo "==> registry reap: deleted ${reg_removed} stale tag manifest(s); garbage-collect FAILED (non-fatal)." >&2
fi
