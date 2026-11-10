#!/usr/bin/env bash
# Download app icons into config/icons (served locally, no hotlinking).
#   ./fetch-icons.sh immich jellyfin home-assistant ...
# Looks in homarr-labs/dashboard-icons (svg, then png), then selfhst/icons (svg).
# Set ICONS_DIR to use another folder (default: ./config/icons next to this script).
set -euo pipefail
dir="${ICONS_DIR:-$(dirname "$0")/config/icons}"
mkdir -p "$dir" && cd "$dir"
[ $# -gt 0 ] || { echo "usage: $0 <icon-name>..." >&2; exit 1; }
rc=0
for n in "$@"; do
  [[ "$n" =~ ^[a-z0-9._-]+$ ]] || { echo "skip invalid name: $n" >&2; rc=1; continue; }
  for u in "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/$n.svg" \
           "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/$n.png" \
           "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/$n.svg"; do
    ext="${u##*.}"
    if curl -fsSL -m 20 "$u" -o "$n.$ext.tmp"; then mv "$n.$ext.tmp" "$n.$ext"; echo "ok  $n.$ext"; continue 2; fi
    rm -f "$n.$ext.tmp"
  done
  echo "not found: $n" >&2; rc=1
done
exit $rc
