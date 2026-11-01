# Optional developer crash diagnosis, never used by acceptance/release jobs.
FROM media-browser-build AS renderer
USER builder
RUN make -C /build/media-browser/src/pqiv clean && \
    make -C /build/media-browser/src/pqiv -j2 GTK_VERSION=3 \
    BACKENDS='gdkpixbuf webp archive archive_cbx poppler spectre wand' \
    BACKENDS_BUILD=static CFLAGS='-O0 -g -Wno-deprecated-declarations' \
    PQIV_VERSION_STRING=2.13.3-media-browser9 pqiv
FROM media-browser-gui
USER root
RUN pacman -S --noconfirm --needed gdb
COPY --from=renderer /build/media-browser/src/pqiv/pqiv /usr/lib/media-browser/pqiv.real
COPY ci/debug-renderer.sh /usr/lib/media-browser/pqiv
RUN chmod 755 /usr/lib/media-browser/pqiv
USER tester
