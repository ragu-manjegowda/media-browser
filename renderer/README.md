# Private renderer provenance

- Upstream: https://github.com/phillipberndt/pqiv
- Revision: `c6e10052d39b798f8d4a48ec4ba6e19647bc275b` (2.13.3)
- License: GPL-3.0-or-later (upstream `LICENSE` is included in packages).
- Patched version: `2.13.3-media-browser9`.
- Patch: `patches/media-browser.patch`; apply with `git apply --unidiff-zero`.
- Integration: `media-browser.c`, included into pqiv's translation unit.

The renderer uses inherited stdin/stdout pipes. Native actions, quick-jump
completion, mouse selection, and configure events report stable catalog IDs and
the actual viewport. Rust owns all originals, selection, scheduling, and Trash.
Native marking addresses a stable ID without moving the cursor. Confirmation
uses NUL-delimited original-path snapshots and GTK's default-No y/n dialog.

The gallery has a GTK search/status bar above its drawing canvas. Rust filters
original relative paths and returns stable IDs with a query revision. Native
montage navigation/drawing uses a weak-reference filtered view; loading and
registry mutation retain the original tree. Revision acknowledgements prevent
stale cursor events from driving original-file operations. Post-removal Enter
waits for the replacement cursor acknowledgement. Canvas events include their
actual origin and dimensions, excluding the search bar. Search input consumes
text normally; grid keys are handled before GTK's default focus navigation.
Destroyed widgets clear their shared pointers before queued state callbacks run.

Catalog publication advances a per-entry generation. Image loading records the
generation it opened, and thumbnail installation is protected by the file-tree
lock and accepted only for the current generation. This prevents an in-flight
placeholder decode from overwriting a newly published poster. Invalidation also
survives publication during loading and queue cancellation; unrelated populated
tile surfaces remain intact.

Photo viewing establishes fit mode before any source decode, rather than
depending on a startup action which can run before an image is loaded. Native
configure events recompute fit for the allocated tile; explicit user zoom sets
the normal scale override and remains effective. The first-only PPM/BMP GUI
regressions cover startup timing for fast-decoding formats.

GTK's normal theme is respected; the upstream forced-dark preference is removed.
Theme colors drive Cairo backgrounds and highlights via GTK style updates.
Unpublished catalog placeholders are not painted as media; theme redraws retain
existing thumbnail surfaces. Media masters and copied pixels remain unchanged.

`mb_help()` builds grouped Gallery/Photo tables from effective native bindings,
including user overrides. A focused entry applies Unicode case-folded fuzzy
matching across keys/actions/sections. Tab focuses the list, j/k scroll outside
the entry, and / returns to it. Clipboard actions resolve gallery stable IDs
in Rust. Photo requests use an inherited connected Unix datagram endpoint to
report the actual browsed original; Rust validates it against the source registry
and forwards an encoded path to the gallery's clipboard owner. No pathname-based
socket or helper interpreter is needed. The GTK owner decodes one full image
asynchronously, with one replaceable pending request, cancellation/generation and
source-fingerprint checks. It publishes real image targets and retains ownership
after an individual photo process closes. GTK asks a clipboard manager to store
data where one is available; no persistent helper process is left after app exit.

Both gallery and photos use the private executable by absolute package path.
GdkPixbuf, WebP, archive, PDF, PostScript and ImageMagick backends are built
statically into that executable; GTK and codec libraries remain ordinary dynamic
Arch dependencies. Video playback is external mpv. The upstream libav backend is
not needed for gallery posters (FFmpeg supplies these in Rust-managed workers).

Changing the pin, patch or backends requires a version update, clean package
rebuild and the GUI acceptance matrix. Releases must retain the upstream source
archive, integration source, patch, Cargo.lock and license provenance.
