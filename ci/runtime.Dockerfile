# Install the exact package artifact produced by the package job into a fresh
# runtime. No source tree, Rust tools or development environment are copied.
FROM archlinux:base
RUN pacman -Syu --noconfirm --needed dbus xorg-server-xvfb awesome picom xdotool \
    xorg-xprop xorg-xwininfo xorg-xrandr python python-gobject python-pillow \
    python-xlib ttf-dejavu procps-ng which chromium xsettingsd
COPY artifacts/packages /packages
ARG PACKAGE_FILE=media-browser-*.pkg.tar.zst
RUN pacman -U --noconfirm /packages/${PACKAGE_FILE} && useradd -m tester
COPY tests /tests
USER tester
WORKDIR /home/tester
ENTRYPOINT ["dbus-run-session", "--", "python", "/tests/gui.py"]
