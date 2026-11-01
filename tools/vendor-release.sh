#!/usr/bin/env bash
# Supplement makepkg --allsource with the exact Rust dependency source/licenses.
set -euo pipefail
manifest=$1
output=$2
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cargo vendor --locked --manifest-path "$manifest" "$stage/vendor" > /dev/null
install -m644 "$(dirname "$manifest")/Cargo.lock" "$stage/Cargo.lock"
tar --sort=name --mtime="@${SOURCE_DATE_EPOCH:-0}" --owner=0 --group=0 --numeric-owner \
    -czf "$output/media-browser-rust-dependencies.tar.gz" -C "$stage" vendor Cargo.lock
