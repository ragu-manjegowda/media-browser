FROM archlinux:base AS build
RUN pacman -Syu --noconfirm --needed base-devel rust git gtk3 gdk-pixbuf2 \
    mpv ffmpeg libwebp libarchive poppler-glib libspectre imagemagick librsvg \
    glycin libheif namcap && useradd -m builder && mkdir /build /out && chown builder /build /out
COPY --chown=builder:builder Cargo.toml Cargo.lock LICENSE PKGBUILD /work/
COPY --chown=builder:builder src /work/src
COPY --chown=builder:builder renderer /work/renderer
COPY --chown=builder:builder defaults /work/defaults
COPY --chown=builder:builder tools /work/tools
COPY --chown=builder:builder ci/check-package.py /work/ci/check-package.py
USER builder
WORKDIR /work
ENV CARGO_HOME=/build/cargo-home
RUN cargo fmt --check && cargo clippy --locked --all-targets -- -D warnings
RUN BUILDDIR=/build bash tools/package.sh /build/package && \
    python ci/check-package.py /build/package/*.pkg.tar.zst && cp /build/package/*.pkg.tar.zst /out/ && \
    cp /build/package/*.src.tar.gz /out/ && \
    bash tools/vendor-release.sh /build/media-browser/src/application/Cargo.toml /out
WORKDIR /out
RUN sha256sum * > SHA256SUMS

# Rebuild the full source package outside the checkout, in an unrelated home.
# Install dependencies from PKGBUILD rather than relying on the build image.
FROM archlinux:base AS source-rebuild
RUN pacman -Syu --noconfirm --needed base-devel && useradd -m sourcebuilder && \
    mkdir -p /rebuild && chown sourcebuilder /rebuild
COPY --from=build /out/*.src.tar.gz /sources/
RUN bsdtar -xf /sources/*.src.tar.gz -C /rebuild && \
    bash -c 'source /rebuild/media-browser/PKGBUILD; pacman -S --noconfirm --needed "${depends[@]}" "${makedepends[@]}" "${checkdepends[@]}"' && \
    chown -R sourcebuilder /rebuild
USER sourcebuilder
WORKDIR /rebuild/media-browser
RUN BUILDDIR=/rebuild/build makepkg --cleanbuild --noconfirm

# No checkout, test harness, development libraries or user configuration.
FROM archlinux:base AS package-runtime
RUN pacman -Syu --noconfirm
COPY --from=build /out/*.pkg.tar.zst /packages/
RUN pacman -U --noconfirm /packages/*.pkg.tar.zst && useradd -m packagecheck && \
    pacman -Qk media-browser && \
    test -f /usr/share/media-browser/pqivrc && test -f /usr/share/media-browser/config.toml
USER packagecheck
ENV HOME=/home/packagecheck
RUN NO_AT_BRIDGE=1 /usr/bin/media-browser --version && \
    NO_AT_BRIDGE=1 /usr/lib/media-browser/pqiv --version && \
    /usr/bin/mpv --version && /usr/bin/ffmpeg -version

FROM archlinux:base AS runtime
RUN pacman -Syu --noconfirm --needed dbus xorg-server-xvfb awesome picom xdotool \
    xorg-xprop xorg-xwininfo xorg-xrandr python python-gobject python-pillow \
    python-xlib ttf-dejavu procps-ng which chromium xsettingsd
COPY --from=build /out /packages
RUN pacman -U --noconfirm /packages/*.pkg.tar.zst && useradd -m tester
COPY tests /tests
USER tester
WORKDIR /home/tester
ENTRYPOINT ["dbus-run-session", "--", "python", "/tests/gui.py"]
