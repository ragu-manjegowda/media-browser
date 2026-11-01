# Testing and acceptance coverage

The suite exercises the installed Rust controller, private GTK/pqiv renderer and
real mpv. All media are generated locally. Tests use private HOME/XDG/cache/Trash
directories, a private D-Bus session, Xvfb and a deterministic Awesome tiled
layout. Awesome and Picom are test fixtures, not application requirements.

## Fast checks

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
ruff check tests ci tools
ruff format --check tests ci tools
bash -n PKGBUILD tools/package.sh tools/vendor-release.sh ci/debug-renderer.sh
```

Rust tests cover selection/range semantics, natural discovery and cycles, fuzzy
matching/stable IDs, GIF block parsing, fingerprints/invalidation, independent
publication, bounded/reprioritized viewport work, three video codecs, translucent
badges and upgrading cached posters without decoding again. They require the
GTK/GIO/GdkPixbuf development libraries and FFmpeg declared in the Arch build.

## Clean package and portability checks

```sh
docker build --target build -t media-browser-build -f ci/Dockerfile .
docker build --target source-rebuild -f ci/Dockerfile .
docker build --target package-runtime -f ci/Dockerfile .
```

`build` runs format/strict Clippy checks, locked unit tests, the pinned native
renderer build, PKGBUILD and namcap validation. It produces binary/full-source
packages, a Rust dependency-source/license archive and checksums under `/out`.
`source-rebuild` extracts the full source package into an unrelated clean Arch
environment and builds it as an ordinary user using declared dependencies.
`package-runtime` installs into fresh Arch base with only declared runtime
dependencies, then verifies package ownership, resources and binary versions.

## Full GUI matrix

```sh
docker build -t media-browser-gui -f ci/Dockerfile .
mkdir -p artifacts
docker run --rm --init --network none --shm-size=512m \
  -e MEDIA_BROWSER_REQUIRE_FULL=1 \
  -v "$PWD/artifacts:/artifacts" media-browser-gui \
  --artifacts /artifacts/run-1024-none --geometry 1024x768 --compositor none
```

The artifact directory must be writable by container UID 1000. Use a fresh
artifact subdirectory for each run. Run all four combinations:

| Geometry | Compositor |
| --- | --- |
| 1024×768 | None |
| 1024×768 | Picom/XRender |
| 2880×1800 | None |
| 2880×1800 | Picom/XRender |

CI builds the package once and tests that exact artifact in fresh runtimes using
`ci/runtime.Dockerfile`. Pull requests and pushes run package/lint/GUI jobs;
release jobs depend on their success. Missing dependencies, skipped required
cases and unexecuted scenarios fail the suite. `--scenario NAME` is available
for targeted debugging; the full-required setting rejects partial runs.

mpv is observed through JSON IPC configured by ordinary discovery in the private
test HOME. Fixtures live under `fixtures/mpv/`. Null audio verifies decoding
without requiring speakers; software video requires no physical GPU. Tests
assert exact original-path playback and preservation of the gallery, not a
particular user's mpv key preferences.

## Scenario map

Every scenario runs in every geometry/compositor combination.

| Scenario | Assertions |
| --- | --- |
| `discovery` | Pictures/HOME defaults, directory/file siblings, recursion/natural order, extensionless media, Unicode/apostrophe paths, duplicate basenames, symlink cycles, own-cache exclusion and XDG overrides |
| `help_theme` | Effective overridden/disabled bindings, modal help close paths, range/mark/cursor preservation, actual light/dark XSettings changes, canvas/dialog pixels and unchanged poster timestamps |
| `help_search` | Focused fuzzy shortcut search, subsequence/no-match/reset states, entry typing vs list j/k scrolling and focus transfer |
| `gallery_search` | Fuzzy filenames/relative folders, stable IDs, duplicate names, hidden marks/Enter blocking, stopped ranges, no-match navigation, filtered original viewing/deletion and clearing |
| `clipboard_paste` | Full resolution, alpha/orientation, Unicode paths, rapid request ordering, photo n/p mapping, ownership after photo close, rejecting video-poster substitution and real Chromium Ctrl+V image pixels |
| `navigation_selection` | Exact selected IDs for keys/aliases/clicks, scrolled/partial rows, quick-label lengths/cancel/timeout, all size steps/clamps, growing/shrinking/disjoint/page/rapid ranges, mark preservation and Enter blocking |
| `fit_lifecycle` | WM tiling/non-ontop/fullscreen state, rendered first-fit for small/large photos before user zoom, image zoom/pan/n/p, focus transitions and viewer-only close paths |
| `first_fit_fast_format` | First/only PPM and BMP fitting initialized before decoding, not a timing-sensitive startup action |
| `confirmation_trash` | Actual GTK default-No cancellation via Enter/n/Escape/window-close, full paths for 1/5 vs count-only 6/8, disjoint original bytes and `.trashinfo`, mark cleanup, post-removal original mapping and repeated last-file persistence |
| `partial_failure` | Real permission failure, GTK error reporting, surviving failed original/mark and no permanent-delete fallback |
| `image_formats_orientation` | Private renderer backend format coverage and embedded orientation |
| `legacy_cache` | Valid unbadged video poster upgrade without decoding and unchanged warm-cache timestamps |
| `video_playback` | H.264 B-frame/10-bit HEVC/VP9 decoded posters, rendered play badge, exact original in real mpv, advancing playback, decoded audio/video and clean close preserving gallery |
| `gif_playback` | Transparent-first-frame poster replacement, uppercase/extensionless GIFs, animated/still dotted-badge metadata, actual changing mpv frames, still photo viewing and Unicode warm-cache reuse |
| `cache_progressive` | 501 mixed entries, geometry-derived visible+one-adjacent bounds, two workers, 525 populated-tile samples with zero blank returns, paging/back/far-jump cancellation, untouched middle, warm reuse and mark/resize interaction |
| `thumbnail_publication_race` | FIFO-paused native placeholder load, newer poster publication before stale decode completion, generation-checked thumbnail installation and unchanged populated neighbor |
| `cache_mutations` | Normal/same-mtime edits, inode replacement, corrupted/removed caches and new sources with affected-entry-only regeneration |
| `stale_generation` | Paused real FFmpeg children, source replacement/deletion during generation, stale-result rejection and current fingerprint publication |
| `cancellation` | Active jobs, rapid direction changes, pending photo launch, confirmation shutdown, runtime cleanup and complete process-group reaping |

The WM fixture includes a generic Pqiv floating/ontop rule with gallery/photo
instance exclusions, testing application window classes as well as geometry.
The large display's 256-pixel grid reproduces 60 visible + 60 prefetched sources;
smaller displays use actual geometry-derived bounds rather than a fixed count.

## Evidence and regression baseline

The 0.3.0 package passed 11 Rust tests and all **19 scenarios × 4 combinations =
76 GUI scenario runs**, including 525 loaded-tile samples per run with zero
blank regressions. Clean Arch package/namcap, standalone source rebuilding and
minimal runtime installation also passed. Test results belong to their tested
revision/package; consult CI artifacts for subsequent changes.

GUI artifacts include `summary.json` with executed scenario names, package/pin
and environment details, native/controller logs, work/cache counters, WM
measurements, mpv assertions, screenshots and failure recordings. Screenshots
support programmatic assertions rather than determining the verdict alone.
The harness guarantees teardown of its viewers, decoders, compositor, WM and
X server; successful runs remove their synthetic HOME after saving evidence.

For native crashes, `ci/debug.Dockerfile` and `ci/debug-renderer.sh` provide an
optional GDB-wrapped renderer image based on the build and GUI images. These
diagnostic tools are separate from the acceptance/release jobs.
