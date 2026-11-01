#!/usr/bin/env bash
# Build the self-contained recipe in a separate working directory.
set -euo pipefail
root=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
destination="$root/.build/package"
if [[ $# -gt 0 && $1 != -* ]]; then
    destination=$1
    shift
fi
mkdir -p "$destination"
install -m644 "$root/PKGBUILD" "$destination/"
makepkg --dir "$destination" --cleanbuild --force "$@"
makepkg --dir "$destination" --allsource --noconfirm --force
