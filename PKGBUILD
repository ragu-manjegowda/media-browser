# Maintainer: Media Browser contributors
pkgname=media-browser
pkgver=0.3.0
pkgrel=1
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
_modules=(main app selection catalog thumbnails cache trash config renderer viewers search)
source=("pqiv::git+https://github.com/phillipberndt/pqiv.git#commit=$_pqiv_revision"
        Cargo.toml Cargo.lock LICENSE media-browser.c media-browser.patch config.toml pqivrc)
for _module in "${_modules[@]}"; do source+=("$_module.rs"); done
# Regenerated using makepkg -g whenever a local source changes.
sha256sums=('SKIP'
            'baf8eb8945a309199b5950adb32389b26933c2f5b71e2e5f8894ee0d0c8edfbf'
            '9ffb4eef621792ef4014481512923bb165a57e62cc1c509dc4898533f2c2dbcf'
            '89fca7ba58af96b0011e92452d51820ef8c2eaa0a7ac365a822617a3b8cfaa17'
            'fb2e8373dbfb0bb4f067a449d9f3631ea68b8ccc9829234fba296f3e5bfa81d7'
            '66892b62e089acdb1f8e9655f0990b5ebb0e8b4fe760a3b60080d79db8b9c29b'
            '6c32f1e744157087f1b7941a97b8f479cbe4f526fbfd7ea6f513d2e60efe3965'
            '6d710f8329a110f8d422bcab4c6b5af61d6a0bb6700850be74f0c5b118729ad2'
            '5e7368baeb16dbc0b55cbb41f3dea6f5cb6b5d3d2af49a974f9434400df66a8d'
            '4fbaa486fdb9de2d2caefe15e10187f1c953900b74aceaa77fb769646b80b817'
            '249cb12a6abf0752618079934f00c86157bbb639f74a605192af9fcbd23237bd'
            'c45b4a4127cb27145c39a0ce45d2530810afa272be91c0cf7c3ea799b8b8f3c0'
            '2cab27c5de8e4e6e15d7ba6676d358708b3c5352b07a48ae2f43744f23c97050'
            'ea22467b481ed65cd7adf4cdce06bf4ca97bc05b2178e03006dec57cea2a4efe'
            '3f446b37faedf840a7626e3b1759eff98328bbe40b50d70180a66f1809c4ae87'
            '4b18fc2266dfa3f8c43a97aee3b8e2f6de153b9995d8b4a4a6853f2811fd3a33'
            '852fe52a4293d880796dad1c67e44945f6737e8c022a5e73ce9cb5f3e1e17fef'
            '001e3f72263b50ac93acf9d1d7b5558160b5292077291e3dcf1ceda684100dab'
            'cdf77d041a33a30bccb9a64e8b1066b5a9b05022f01d33019cd15871079c048c')

prepare() {
    export PATH=/usr/bin:/bin
    [[ "$(git -C "$srcdir/pqiv" rev-parse HEAD)" == "$_pqiv_revision" ]]
    git -C "$srcdir/pqiv" apply --unidiff-zero "$srcdir/media-browser.patch"
    install -m644 "$srcdir/media-browser.c" "$srcdir/pqiv/media-browser.c"
    mkdir -p "$srcdir/application/src"
    install -m644 "$srcdir/Cargo.toml" "$srcdir/Cargo.lock" "$srcdir/application/"
    for _module in "${_modules[@]}"; do
        install -m644 "$srcdir/$_module.rs" "$srcdir/application/src/$_module.rs"
    done
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
    install -Dm644 "$srcdir/config.toml" "$pkgdir/usr/share/media-browser/config.toml"
    install -Dm644 "$srcdir/pqivrc" "$pkgdir/usr/share/media-browser/pqivrc"
    install -Dm644 "$srcdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 "$srcdir/pqiv/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/COPYING.pqiv"
}
