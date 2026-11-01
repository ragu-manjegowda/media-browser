"""Ensure the pinned package archive builds the application's current sources."""

import sys
from pathlib import Path

checkout, packaged = map(Path, sys.argv[1:])
files = {
    Path("Cargo.toml"),
    Path("Cargo.lock"),
    Path("LICENSE"),
    Path("renderer/media-browser.c"),
    Path("renderer/patches/media-browser.patch"),
}
for root in (checkout, packaged):
    for directory in ("src", "defaults"):
        files.update(
            path.relative_to(root) for path in (root / directory).rglob("*") if path.is_file()
        )
different = [
    str(path)
    for path in sorted(files)
    if not (checkout / path).is_file()
    or not (packaged / path).is_file()
    or (checkout / path).read_bytes() != (packaged / path).read_bytes()
]
if different:
    raise SystemExit(
        "Pinned package sources differ from the checkout; update the published "
        "application revision and archive checksum in PKGBUILD:\n" + "\n".join(different)
    )
print(f"PASS: all {len(files)} application inputs match the pinned package archive")
