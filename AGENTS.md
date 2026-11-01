# Media Browser development guide

Media Browser is an X11 photo/video gallery. A Rust controller owns application
state and original files; a private patched pqiv (C/GTK 3/Cairo) renders galleries
and photos; mpv plays videos and animated GIFs. Python is test tooling only.

## Architecture and code map

| Area | Files | Responsibility |
| --- | --- | --- |
| Application loop | `src/main.rs`, `src/app.rs` | Launch, event ordering, selection/view synchronization, original-file operations and shutdown |
| Source registry | `src/catalog.rs` | Recursive natural-order discovery, media/GIF detection, stable IDs and atomic session PNG publication |
| Filtering | `src/search.rs` | Case-insensitive fuzzy matching of filenames and relative folders, preserving natural order and IDs |
| Selection | `src/selection.rs` | Range anchor, preserved marks, range shrinking and open eligibility |
| Thumbnail work | `src/thumbnails.rs` | Viewport planning, two workers, reprioritization, cancellation and process deadlines |
| Persistent cache | `src/cache.rs` | Source fingerprints, validated 512-pixel masters, decoding, orientation and video/GIF badges |
| Native bridge | `src/renderer.rs` | Private pqiv version verification, action pipe, JSON event reader and child lifecycle |
| Native UI | `renderer/media-browser.c`, `renderer/patches/media-browser.patch` | Gallery canvas/search, filtered native view, input/viewport events, per-entry reload, GTK confirmation/help/clipboard and first-open fitting |
| Viewers and Trash | `src/viewers.rs`, `src/trash.rs` | Original-file photo/mpv launches and GIO Freedesktop Trash |
| Configuration | `src/config.rs`, `defaults/` | Installed defaults, optional XDG overrides and renderer version contract |
| Packaging/CI | `PKGBUILD`, `tools/`, `ci/`, `.github/workflows/ci.yml` | Locked builds, source/license archives, clean Arch packaging and installed-package acceptance |

### Controller/renderer protocol

- Rust sends native actions over inherited stdin; the gallery emits JSON events
  over stdout. Catalog PNG basenames encode stable source IDs, not tree ranks.
- Search query/view revisions gate cursor operations. The renderer holds a
  weak-reference filtered view over the full registry. Rust schedules only the
  filtered viewport and waits for view acknowledgement after query/removal changes.
  A post-trash Enter resolves against the acknowledged replacement cursor.
- Photo clipboard requests use an inherited connected Unix datagram endpoint.
  Rust validates the browsed original and forwards it to the gallery's GTK
  clipboard owner. Closing a photo does not discard clipboard data while the
  gallery remains alive.
- GTK/Cairo work runs on GTK's main thread. Destroyed search widgets must clear
  shared pointers before queued callbacks can access them. Native thumbnail
  generations reject stale placeholder decodes and stale surface installation.

## Behavioral invariants

- Original paths drive viewing, copying, confirmation and Trash. Session PNGs
  are display-only. Filtering/removal must never confuse stable IDs with ranks.
- Escape stops a visual range and preserves marks; `u` clears all marks. Another
  range can add disjoint marks. Hidden marks still block Enter and join batch
  Trash. Filtering stops the active range without clearing marks.
- Trash uses one default-No GTK confirmation. One through five originals show
  full paths; larger batches show only the count. Cancellation preserves marks;
  partial failures retain failed originals and report errors. No permanent-delete
  fallback. An empty gallery stays open until explicit quit.
- Gallery/photo clients start as ordinary non-ontop windows. Photos fit their
  allocated geometry before any user zoom. Opening/closing a viewer preserves
  the gallery and cursor. mpv owns its ordinary configuration and scripts.
- Thumbnail sizes are 64, 96, 128, 192, 256 (default), and 384. Grid `f` is native
  quick-jump, not fullscreen; `/` focuses gallery search. Entry letters remain
  text; grid keys must not trigger GTK focus-chain navigation.
- Cache work is bounded to the visible page plus at most one adjacent page in
  the direction of travel, with two workers. Far jumps cancel obsolete work.
  Warm masters and thumbnail-size changes must not repeatedly decode originals.
- Fingerprints include path/URI, byte size, device/inode and nanosecond mtime/ctime.
  Preserve the private MD5-of-unescaped-file-URI filename namespace unless making
  an explicit schema migration; PNG URI metadata is escaped ASCII. Publish
  atomically and reject results if the source changed during decoding.
- Reload only the completed tile. Never clear populated tiles or marks as other
  jobs finish. Videos use a translucent play badge; all GIFs use a dotted-circle
  badge. Animated GIF posters must contain a visible frame; still GIFs remain
  photos, animated GIFs open in mpv.
- Shutdown cancels/joins workers, reaps viewers/decoders and removes session IPC.
  Native GTK theming must not recolor media or invalidate populated posters.

## Packaging and renderer changes

One Arch package owns `/usr/bin/media-browser`, `/usr/lib/media-browser/pqiv`,
and immutable defaults under `/usr/share/media-browser`. User overrides are
optional under `$XDG_CONFIG_HOME/media-browser`; cache is under
`$XDG_CACHE_HOME/media-browser`. Runtime execution needs neither this checkout,
Python, home-directory helpers nor a specific window manager.

The exact pqiv pin, backends and GPL-3.0-or-later provenance are documented in
[`renderer/README.md`](renderer/README.md). Apply the patch to that pin using
`git apply --unidiff-zero`. Edit upstream call sites in a separate pinned source
checkout and regenerate the patch; keep integration code in `media-browser.c`.
Update `PKGBUILD` source hashes whenever a packaged source changes. New Rust
modules must be added to `_modules`. Keep native version strings synchronized
between `PKGBUILD` and `src/config.rs` when changing the renderer contract.

Private pqiv pins the application/API, not GTK or codec library ABIs. Build
against current Arch dependencies and bump `pkgrel` for ABI rebuilds. Keep
`Cargo.lock`, locked builds, source archives and license provenance intact.

## Development and verification

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
ruff check tests ci tools
ruff format --check tests ci tools
bash -n PKGBUILD tools/package.sh tools/vendor-release.sh ci/debug-renderer.sh
```

Use `bash tools/package.sh --syncdeps` for a local package build. Cargo-only
builds do not install the renderer/defaults needed for application execution.
[`tests/README.md`](tests/README.md) documents the full installed-package GUI
matrix, scenario coverage, portable fixtures and artifact collection.

For renderer, protocol, selection, scheduling or viewer changes, exercise the
actual installed package with GTK/pqiv/mpv in the full four-way GUI matrix.
Missing dependencies and unexecuted required scenarios are failures; unit tests
or screenshots alone do not substitute for end-to-end interaction. Preserve
the existing acceptance scenarios when extending tests. Use generated media and
private HOME/XDG/Trash directories. `MEDIA_BROWSER_TRACE` enables opt-in event,
cache/work and native diagnostic evidence; logs can contain original paths.
