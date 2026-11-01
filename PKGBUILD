# Maintainer: Media Browser contributors
pkgname=media-browser
pkgver=0.3.0
pkgrel=2
pkgdesc='Viewport-bounded native photo/video gallery using private pqiv and normal mpv'
arch=('x86_64')
url='https://github.com/ragu-manjegowda/media-browser'
license=('GPL-3.0-or-later')
depends=('gtk3' 'gdk-pixbuf2' 'glib2' 'mpv' 'ffmpeg' 'libwebp' 'libarchive'
         'glibc' 'libgcc' 'libx11' 'cairo' 'pango' 'zlib' 'at-spi2-core' 'harfbuzz'
         'poppler-glib' 'libspectre' 'imagemagick' 'librsvg' 'glycin' 'libheif'
         'libgtk-3.so' 'libgdk-3.so' 'libgdk_pixbuf-2.0.so' 'libgio-2.0.so'
         'libglib-2.0.so' 'libgobject-2.0.so')
makedepends=('rust' 'cargo' 'git' 'pkgconf')
checkdepends=('ffmpeg')
options=('!lto' '!debug')
_pqiv_revision=c6e10052d39b798f8d4a48ec4ba6e19647bc275b
_application_revision=fef6210139e9f87c48942c27c3cb37f25247e4ba
source=("media-browser-$_application_revision.tar.gz::$url/archive/$_application_revision.tar.gz"
        "pqiv::git+https://github.com/phillipberndt/pqiv.git#commit=$_pqiv_revision")
sha256sums=('669bf0cfc2497282b7daa7d2d91343e87c2deae41899f7fbf0282f16f38a700d'
            'SKIP')

# A checkout's src/ contains Rust sources, not disposable makepkg output.
if [[ -n ${startdir:-} && ${BUILDDIR:-} -ef $startdir && -f $startdir/src/main.rs ]]; then
    BUILDDIR="$startdir/.build"
fi

prepare() {
    export PATH=/usr/bin:/bin
    rm -rf "$srcdir/application"
    cp -a "$srcdir/media-browser-$_application_revision" "$srcdir/application"
    [[ "$(git -C "$srcdir/pqiv" rev-parse HEAD)" == "$_pqiv_revision" ]]
    git -C "$srcdir/pqiv" apply --unidiff-zero "$srcdir/application/renderer/patches/media-browser.patch"
    install -m644 "$srcdir/application/renderer/media-browser.c" "$srcdir/pqiv/media-browser.c"
    export CARGO_HOME="$srcdir/cargo-home"
    cargo fetch --locked --manifest-path "$srcdir/application/Cargo.toml"
}

build() {
    export PATH=/usr/bin:/bin
    export PKG_CONFIG_PATH=
    export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig
    export CARGO_HOME="$srcdir/cargo-home"
    # Package paths must not inherit development-only relocations.
    export MEDIA_BROWSER_LIBDIR=/usr/lib/media-browser
    export MEDIA_BROWSER_DATADIR=/usr/share/media-browser
    cargo build --frozen --release --manifest-path "$srcdir/application/Cargo.toml"
    make -C "$srcdir/pqiv" GTK_VERSION=3 \
        BACKENDS='gdkpixbuf webp archive archive_cbx poppler spectre wand' \
        BACKENDS_BUILD=static PQIV_VERSION_STRING=2.13.3-media-browser9 pqiv
}

check() {
    export PATH=/usr/bin:/bin
    export CARGO_HOME="$srcdir/cargo-home"
    export HOME="$srcdir/check-home"
    export XDG_CONFIG_HOME="$HOME/config" XDG_CACHE_HOME="$HOME/cache" XDG_DATA_HOME="$HOME/data"
    mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
    cargo test --frozen --manifest-path "$srcdir/application/Cargo.toml"
    [[ "$(NO_AT_BRIDGE=1 "$srcdir/pqiv/pqiv" --version)" == 'pqiv 2.13.3-media-browser9' ]]
}

package() {
    install -Dm755 "$srcdir/application/target/release/media-browser" "$pkgdir/usr/bin/media-browser"
    install -Dm755 "$srcdir/pqiv/pqiv" "$pkgdir/usr/lib/media-browser/pqiv"
    install -Dm644 "$srcdir/application/defaults/config.toml" "$pkgdir/usr/share/media-browser/config.toml"
    install -Dm644 "$srcdir/application/defaults/pqivrc" "$pkgdir/usr/share/media-browser/pqivrc"
    install -Dm644 "$srcdir/application/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 "$srcdir/pqiv/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/COPYING.pqiv"
}
