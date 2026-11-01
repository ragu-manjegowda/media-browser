#!/usr/bin/env bash
# Stage makepkg's flat local sources without mixing its src/ with Rust's src/.
set -euo pipefail
root=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
destination="$root/.build/package"
if [[ $# -gt 0 && $1 != -* ]]; then
    destination=$1
    shift
fi
mkdir -p "$destination"
install -m644 "$root/PKGBUILD" "$root/Cargo.toml" "$root/Cargo.lock" "$root/LICENSE" "$destination/"
install -m644 "$root"/src/*.rs "$root/renderer/media-browser.c" \
    "$root/renderer/patches/media-browser.patch" "$root/defaults/config.toml" \
    "$root/defaults/pqivrc" "$destination/"
makepkg --dir "$destination" --cleanbuild --force "$@"
makepkg --dir "$destination" --allsource --noconfirm --force
