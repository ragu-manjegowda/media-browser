#!/usr/bin/env python3
"""Real installed-application GUI acceptance tests; no mocked viewers or skips.

Run under dbus-run-session. Every process, HOME, media source and Trash belongs
to this harness. --binary is provided only for explicitly labelled dev runs;
CI always requires /usr/bin/media-browser and its package-owned renderer.
"""

import argparse
import base64
import configparser
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import gi
from PIL import Image, ImageChops, ImageStat
from Xlib import X, display

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402 (GI version must be selected first)


def wait(check, description, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError(f"Timed out: {description}")


class Suite:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.artifacts).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.home = self.out / "home"
        self.home.mkdir()
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            NO_AT_BRIDGE="1",
            GDK_BACKEND="x11",
            LIBGL_ALWAYS_SOFTWARE="1",
        )
        for key in ("PQIVRC_PATH", "MEDIA_BROWSER_EVENTS", "MPV_HOME", "GTK_THEME"):
            self.env.pop(key, None)
        for key, relative in [
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_DATA_HOME", "data"),
            ("XDG_STATE_HOME", "state"),
            ("XDG_RUNTIME_DIR", "runtime"),
        ]:
            path = self.home / relative
            path.mkdir(mode=0o700)
            self.env[key] = str(path)
        self.processes = []
        self.apps = []
        self.ran = []
        self.success = False
        self.x = None
        self.start_display()
        self.wm = self.spawn(
            ["awesome", "-c", str(Path(__file__).with_name("awesome.lua"))], "awesome"
        )
        wait(
            lambda: self.x.screen().root.get_full_property(
                self.x.intern_atom("_NET_SUPPORTING_WM_CHECK"), X.AnyPropertyType
            ),
            "Awesome readiness",
        )
        self.xsettings_file = self.out / "xsettings.conf"
        self.xsettings_file.write_text('Net/ThemeName "Adwaita"\n')
        self.xsettings = self.spawn(["xsettingsd", "-c", str(self.xsettings_file)], "xsettings")
        wait(
            lambda: self.x.get_selection_owner(self.x.intern_atom("_XSETTINGS_S0")),
            "XSettings readiness",
        )
        if args.compositor == "picom":
            self.spawn(
                ["picom", "--backend", "xrender", "--config", "/dev/null", "--no-vsync"], "picom"
            )
            wait(
                lambda: self.x.get_selection_owner(self.x.intern_atom("_NET_WM_CM_S0")),
                "Picom readiness",
            )
        self.spawn(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-f",
                "x11grab",
                "-framerate",
                "4",
                "-video_size",
                args.geometry,
                "-i",
                self.env["DISPLAY"],
                "-vf",
                "scale=1024:-2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "30",
                "-threads",
                "1",
                str(self.out / "screen-recording.mp4"),
            ],
            "screen-recording",
        )
        (self.out / "environment.json").write_text(
            json.dumps(
                {
                    "application": self.cmd(args.binary, "--version"),
                    "renderer": self.cmd("/usr/lib/media-browser/pqiv", "--version"),
                    "packages": self.cmd(
                        "pacman", "-Q", "media-browser", "gtk3", "gdk-pixbuf2", "mpv", "ffmpeg"
                    ),
                    "revision": os.environ.get("MEDIA_BROWSER_REVISION", "local-development"),
                    "geometry": args.geometry,
                    "compositor": args.compositor,
                },
                indent=2,
            )
        )
        mpv = Path(self.env["XDG_CONFIG_HOME"]) / "mpv"
        mpv.mkdir()
        self.mpv_socket = self.home / "mpv.sock"
        fixtures = Path(__file__).parent / "fixtures/mpv"
        (mpv / "mpv.conf").write_text(
            (fixtures / "mpv.conf").read_text()
            + f"\nao=null\nvo=x11\ninput-ipc-server={self.mpv_socket}\nkeep-open=yes\nosd-level=0\n"
        )
        shutil.copyfile(fixtures / "input.conf", mpv / "input.conf")
        # These headless observation settings use ordinary mpv discovery and
        # are never app defaults. User keymaps/preferences are outside parity.

    def spawn(self, command, name, env=None, **kwargs):
        log = open(self.out / f"{name}.log", "ab", buffering=0)
        try:
            p = subprocess.Popen(
                command,
                env=env or self.env,
                stdout=log,
                stderr=log,
                start_new_session=True,
                **kwargs,
            )
        finally:
            log.close()
        self.processes.append(p)
        return p

    def start_display(self):
        readfd, writefd = os.pipe()
        try:
            self.spawn(
                [
                    "Xvfb",
                    "-displayfd",
                    str(writefd),
                    "-screen",
                    "0",
                    f"{self.args.geometry}x24",
                    "-nolisten",
                    "tcp",
                    "-ac",
                ],
                "xvfb",
                pass_fds=(writefd,),
            )
            os.close(writefd)
            writefd = None
            import select

            assert select.select([readfd], [], [], 10)[0], "Xvfb readiness deadline"
            number = os.read(readfd, 32).decode().strip()
        finally:
            os.close(readfd)
            if writefd is not None:
                os.close(writefd)
        self.env["DISPLAY"] = f":{number}"
        self.x = display.Display(self.env["DISPLAY"])

    def cmd(self, *command):
        return subprocess.check_output(command, env=self.env, text=True, timeout=10).strip()

    def windows(self, instance=None, title=None):
        # Keep races with destroyed clients out of python-xlib's synchronous
        # reply queue. These X11 query tools have explicit subprocess deadlines.
        if instance is not None or title is not None:
            option = (
                "--class"
                if instance in ("mpv", "media-browser-paste-test")
                else "--classname"
                if instance
                else "--name"
            )
            pattern = "^" + re.escape(instance) + "$" if instance else re.escape(title)
            result = subprocess.run(
                ["xdotool", "search", "--onlyvisible", option, pattern],
                env=self.env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            assert result.returncode in (0, 1), result.stderr
            return [int(w) for w in result.stdout.split()]
        result = self.cmd("xprop", "-root", "_NET_CLIENT_LIST")
        return [int(w, 16) for w in re.findall(r"0x[0-9a-fA-F]+", result)]

    def focus(self, wid):
        result = self.cmd(
            "awesome-client",
            f"for _,c in ipairs(client.get()) do if c.window == {wid} then c:emit_signal('request::activate', 'acceptance', {{raise=true}}) end end",
        )
        assert "error" not in result.lower(), result
        wait(
            lambda: self.cmd("xdotool", "getactivewindow") == str(wid),
            "WM focus assignment",
            timeout=10,
        )

    def key(self, wid, *keys):
        self.focus(wid)
        self.cmd("xdotool", "key", "--clearmodifiers", "--delay", "30", *keys)

    def shot(self, wid, name):
        image = self.raw_image(wid)
        image.save(self.out / f"{name}.png")
        return image

    def image(self, wid):
        image = self.raw_image(wid)
        app = next((app for app in self.apps if app.wid == wid), None)
        state = app.state() if app else None
        if state:
            viewport = state["native"]
            x, y = viewport.get("origin_x", 0), viewport.get("origin_y", 0)
            width, height = viewport.get("viewport_width", 0), viewport.get("viewport_height", 0)
            if (
                width > 0
                and height > 0
                and x >= 0
                and y >= 0
                and x + width <= image.width
                and y + height <= image.height
            ):
                return image.crop((x, y, x + width, y + height))
        return image

    def raw_image(self, wid):
        w = self.x.create_resource_object("window", wid)
        g = w.get_geometry()
        raw = w.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xFFFFFFFF)
        return Image.frombytes("RGB", (g.width, g.height), raw.data, "raw", "BGRX")

    def wm_state(self, wid):
        result = self.cmd(
            "awesome-client",
            f"local c; for _,v in ipairs(client.get()) do if v.window == {wid} then c=v end end; if not c then return 'missing' end; local g=c:geometry(); return tostring(c.floating)..','..tostring(c.ontop)..','..tostring(c.fullscreen)..','..g.x..','..g.y..','..g.width..','..g.height",
        )
        assert "error" not in result.lower(), result
        with open(self.out / "wm-state.log", "a") as f:
            f.write(f"{wid}: {result}\n")
        return result

    def tiled(self, wid):
        assert "false,false,false" in self.wm_state(wid)

    def photo(self, path, size=(120, 80), color=(30, 150, 220)):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path, format="PPM" if not path.suffix else None)

    def video(self, path, codec="h264", seconds=40, size="160x96"):
        options = {
            "h264": ["-c:v", "libx264", "-preset", "ultrafast", "-bf", "3"],
            "hevc": [
                "-c:v",
                "libx265",
                "-pix_fmt",
                "yuv420p10le",
                "-x265-params",
                "pools=1:frame-threads=1:log-level=error",
            ],
            "vp9": ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8"],
        }[codec]
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={size}:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                str(seconds),
                *options,
                "-c:a",
                "libopus" if codec == "vp9" else "aac",
                "-threads",
                "1",
                str(path),
            ],
            env=self.env,
            check=True,
            capture_output=True,
            timeout=90,
        )

    def start(self, directory=None, name=None):
        name = name or f"app-{len(self.apps)}"
        trace = self.out / f"{name}.jsonl"
        env = dict(self.env, MEDIA_BROWSER_TRACE=str(trace))
        p = self.spawn(
            [self.args.binary, *([] if directory is None else [str(directory)])], name, env=env
        )
        app = App(self, p, trace)
        self.apps.append(app)
        app.wid = wait(lambda: self.windows("media-browser-gallery"), "gallery window")[0]
        wait(
            lambda: app.state() is not None and app.state()["native"]["count"] > 0,
            "native viewport",
        )
        return app

    def close(self):
        if getattr(self, "paste_server", None):
            self.paste_server.shutdown()
            self.paste_server.server_close()
            self.paste_thread.join(timeout=5)
        for app in getattr(self, "apps", []):
            if app.p.poll() is None:
                app.p.terminate()
        for p in reversed(getattr(self, "processes", [])):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=5)
        if getattr(self, "x", None):
            try:
                self.x.close()
            except Exception:
                pass  # Xvfb has been reaped, even if its connection closed first.
            self.x = None
        if getattr(self, "success", False):
            shutil.rmtree(self.home)

    def finish(self):
        posters = Path(self.env["XDG_CACHE_HOME"]) / "media-browser/thumbnails/posters"
        metadata = []
        for path in sorted(posters.glob("*.png")):
            with Image.open(path) as image:
                metadata.append(
                    {
                        "name": path.name,
                        "size": image.size,
                        "metadata": image.info,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        trash = Path(self.env["XDG_DATA_HOME"]) / "Trash"
        records = [
            {"name": p.name, "text": p.read_text()}
            for p in sorted((trash / "info").glob("*.trashinfo"))
        ]
        (self.out / "cache-metadata.json").write_text(json.dumps(metadata, indent=2))
        (self.out / "trash-info.json").write_text(json.dumps(records, indent=2))
        self.success = True

    def scenario(self, name, function):
        print(f"RUN {name}", flush=True)
        signal.alarm(180)
        try:
            function()
        finally:
            signal.alarm(0)
        self.ran.append(name)
        (self.out / "summary.json").write_text(
            json.dumps(
                {
                    "ran": self.ran,
                    "count": len(self.ran),
                    "geometry": self.args.geometry,
                    "compositor": self.args.compositor,
                    "binary": self.args.binary,
                    "renderer_pin": "c6e10052d39b798f8d4a48ec4ba6e19647bc275b",
                },
                indent=2,
            )
        )

    def discovery(self):
        pictures = self.home / "Pictures"
        pictures.mkdir()
        for name in (
            "image10.png",
            "image2.png",
            "photo one's",
            "unicodé.png",
            "nested/same.png",
            "other/same.png",
        ):
            self.photo(pictures / name)
        (pictures / "cycle").symlink_to(pictures, target_is_directory=True)
        cache = Path(self.env["XDG_CACHE_HOME"]) / "media-browser/thumbnails"
        self.photo(cache / "not-original.png")
        app = self.start()
        paths = app.paths()
        assert (
            len(paths) == 6 and paths[0].name == "image2.png" and paths[1].name == "image10.png"
        ), paths
        assert cache / "not-original.png" not in paths
        wait(lambda: app.poster(pictures / "photo one's").exists(), "extensionless original cached")
        app.quit()
        app = self.start(pictures / "image2.png")
        assert len(app.paths()) == 4, app.paths()
        app.quit()
        pictures.rename(self.home / "renamed-pictures")
        app = self.start()
        assert len(app.paths()) == 6, app.paths()  # HOME fallback and private cache exclusion
        app.quit()
        user = Path(self.env["XDG_CONFIG_HOME"]) / "media-browser"
        user.mkdir()
        (user / "config.toml").write_text("thumbnail_size = 96\n")
        (user / "pqivrc").write_text("[keybindings]\n@MONTAGE { x { mb_event(clear); } }\n")
        app = self.start(self.home / "renamed-pictures")
        wait(lambda: app.state()["native"]["size"] == 96, "XDG override")
        app.quit()
        (user / "config.toml").unlink()
        (user / "pqivrc").unlink()

    def help_theme(self):
        colors = {"Light": ("#fdf6e3", "#657b83"), "Dark": ("#002b36", "#839496")}
        for variant, (background, foreground) in colors.items():
            theme = self.home / ".themes" / ("MediaBrowserTest" + variant) / "gtk-3.0"
            theme.mkdir(parents=True)
            (theme / "gtk.css").write_text(
                '@import url("resource:///org/gtk/libgtk/theme/Adwaita/gtk-contained.css");\n'
                f"@define-color theme_base_color {background};\n@define-color theme_bg_color {background};\n"
                f"@define-color theme_text_color {foreground};\n@define-color theme_fg_color {foreground};\n"
                "@define-color theme_selected_bg_color #268bd2;\n@define-color theme_selected_fg_color #fdf6e3;\n"
                "* {color: @theme_fg_color;}\n"
                "window, .background, dialog, box, treeview.view, notebook, notebook stack, viewport {background-color: @theme_bg_color;}\n"
                "button {background-image:none; background-color:shade(@theme_bg_color,1.1); color:@theme_fg_color;}\n"
            )

        def select(variant):
            self.xsettings_file.write_text(f'Net/ThemeName "MediaBrowserTest{variant}"\n')
            self.xsettings.send_signal(signal.SIGHUP)

        select("Light")
        user = Path(self.env["XDG_CONFIG_HOME"]) / "media-browser"
        user.mkdir(exist_ok=True)
        (user / "pqivrc").write_text(
            "[keybindings]\n@MONTAGE { u { nop(); } x { mb_event(clear); } }\n"
        )
        folder = self.home / "help-theme"
        for i in range(2):
            self.photo(folder / f"image{i}.png")
        app = self.start(folder)
        wait(lambda: len(app.posters()) == 2, "theme test posters")
        stamps = {p: p.stat().st_mtime_ns for p in app.posters()}
        self.key(app.wid, "v", "l")
        app.marks({0, 1})
        before = (app.state()["cursor"], app.state()["anchor"])

        def theme_is(variant):
            state = app.ini("theme-state.ini")
            expected = tuple(bytes.fromhex(colors[variant][0][1:]))
            if not state or state["theme"]["name"] != "MediaBrowserTest" + variant:
                return False
            assert state["theme"].getboolean("prefer_dark") is False
            return all(
                abs(a - b) <= 1 for a, b in zip(self.image(app.wid).getpixel((2, 2)), expected)
            )

        wait(lambda: theme_is("Light"), "GTK light theme applied to canvas")
        self.shot(app.wid, "gallery-theme-light")
        self.key(app.wid, "question")
        help_window = wait(
            lambda: self.windows(title="Media Browser — Shortcuts"), "native help dialog"
        )[0]
        text = (app.session() / "help-state.tsv").read_text()
        gallery = text.split("[Gallery]\n")[1].split("[Photo]")[0]
        assert "Copy original image" in text and "Copy original full path" in text
        assert "x \tClear every mark and range" in gallery
        assert "u \tClear every mark" not in gallery
        self.shot(help_window, "help-theme-light")

        def dialog_uses(variant):
            image = self.image(help_window)
            expected = tuple(bytes.fromhex(colors[variant][0][1:]))
            count = sum(
                1
                for y in range(0, image.height, 8)
                for x in range(0, image.width, 8)
                if all(abs(a - b) <= 2 for a, b in zip(image.getpixel((x, y)), expected))
            )
            return count > image.width * image.height / 64 * 0.3

        wait(lambda: dialog_uses("Light"), "actual help widgets use light theme")
        select("Dark")
        wait(lambda: theme_is("Dark"), "running gallery follows system dark theme")
        wait(lambda: dialog_uses("Dark"), "actual help widgets follow dark theme")
        self.shot(help_window, "help-theme-dark")
        self.key(help_window, "Escape")
        wait(lambda: not self.windows(title="Media Browser — Shortcuts"), "help Escape close")
        app.marks({0, 1})
        assert (app.state()["cursor"], app.state()["anchor"]) == before
        for close in ("q", "question", "alt+c"):
            self.key(app.wid, "question")
            help_window = wait(
                lambda: self.windows(title="Media Browser — Shortcuts"), "help reopen"
            )[0]
            self.key(help_window, "Tab", close)
            wait(lambda: not self.windows(title="Media Browser — Shortcuts"), "help-only close")
            assert app.p.poll() is None
        self.key(app.wid, "x")
        app.marks(set())
        select("Light")
        wait(lambda: theme_is("Light"), "switch back to light")
        assert all(p.stat().st_mtime_ns == stamp for p, stamp in stamps.items()), (
            "theme change must not regenerate posters"
        )
        self.key(app.wid, "Return")
        viewer = wait(lambda: self.windows("media-browser-viewer"), "photo help viewer")[0]
        self.key(viewer, "question")
        help_window = wait(lambda: self.windows(title="Media Browser — Shortcuts"), "photo help")[0]
        self.key(help_window, "Escape")
        assert viewer in self.windows("media-browser-viewer")
        self.key(viewer, "q")
        app.quit()
        (user / "pqivrc").unlink()
        self.xsettings_file.write_text('Net/ThemeName "Adwaita"\n')
        self.xsettings.send_signal(signal.SIGHUP)

    def help_search(self):
        folder = self.home / "help-search"
        for index in range(2):
            self.photo(folder / f"image{index}.png")
        app = self.start(folder)
        self.key(app.wid, "v", "l")
        app.marks({0, 1})
        before = (app.state()["cursor"], app.state()["anchor"])
        self.key(app.wid, "question")
        window = wait(lambda: self.windows(title="Media Browser — Shortcuts"), "fuzzy help menu")[0]

        def state():
            value = app.ini("help-filter-state.ini")
            return value["help"] if value else None

        wait(lambda: state() and state()["focus"] == "search", "search input focused by default")
        original_count = state().getint("gallery_count")
        self.key(window, "j", "k")
        wait(lambda: state()["query"] == "jk", "j/k are input text while searching")
        assert app.p.poll() is None
        self.key(window, "ctrl+a", "c", "p", "i", "m", "g")
        wait(
            lambda: state()["query"] == "cpimg" and state().getint("gallery_count") == 1,
            "fuzzy action query",
        )
        visible = (
            (app.session() / "help-filter.tsv")
            .read_text()
            .split("[Gallery]\n")[1]
            .split("[Photo]")[0]
        )
        assert "Copy original image" in visible and "Move left" not in visible
        assert "cpimg" not in visible.casefold(), (
            "this exercises subsequence matching, not a literal substring"
        )
        self.shot(window, "help-fuzzy-search")
        self.key(window, "ctrl+a")
        self.cmd("xdotool", "type", "--clearmodifiers", "--delay", "0", "xyzzzz_no_match_123")
        wait(
            lambda: state().getint("gallery_count") == 0 and state().getint("photo_count") == 0,
            "no-match query",
        )
        self.shot(window, "help-no-matches")
        self.key(window, "ctrl+a", "BackSpace")
        wait(
            lambda: state()["query"] == "" and state().getint("gallery_count") == original_count,
            "clearing search restores effective shortcuts",
        )
        self.key(window, "Tab")
        wait(lambda: state()["focus"] == "list", "Tab focuses shortcut list")

        def scroll(key):
            before = state().getfloat("scroll")
            self.key(window, key)
            wait(
                lambda: (
                    state().getfloat("scroll") > before
                    if key == "j"
                    else state().getfloat("scroll") < before
                ),
                f"{key} scrolls help {'down' if key == 'j' else 'up'}",
            )

        # xdotool returning only confirms input delivery. A partial batch of j
        # updates must not become the baseline for the following k assertions.
        for key in ("j", "j", "j", "k", "k"):
            scroll(key)
        assert state()["query"] == ""
        self.key(window, "slash", "q")
        wait(
            lambda: state()["focus"] == "search" and state()["query"] == "q",
            "/ returns to search; q is text",
        )
        self.key(window, "Escape")
        wait(
            lambda: not self.windows(title="Media Browser — Shortcuts"), "Escape closes search/help"
        )
        app.marks({0, 1})
        assert (app.state()["cursor"], app.state()["anchor"]) == before
        app.quit()

    def gallery_search(self):
        folder = self.home / "gallery-search"
        for index in range(240):
            self.photo(folder / "roll" / f"frame{index}.png")
        self.photo(folder / "a" / "same.png")
        self.photo(folder / "b" / "same.png")
        target = folder / "Trips" / "2024" / "Alpine Morning α.jpg"
        self.photo(target, (960, 1280), (40, 180, 90))
        app = self.start(folder)
        target_id = app.paths().index(target)
        self.key(app.wid, "v")
        app.marks({0})
        self.key(app.wid, "slash")
        self.cmd("xdotool", "type", "--clearmodifiers", "--delay", "0", "alp mrn")
        wait(
            lambda: (
                app.state().get("query") == "alp mrn"
                and app.state().get("visible") == [target_id]
                and app.state()["native"]["total"] == 1
            ),
            "fuzzy gallery filename filtering",
        )
        app.cursor(target_id)
        app.marks({0})
        assert app.state()["anchor"] is None
        wait(lambda: app.poster(target).exists(), "far search result poster")
        assert app.state()["work"]["wanted"] == [target_id]
        self.shot(app.wid, "gallery-fuzzy-search")
        self.key(app.wid, "Return")  # Input activation focuses the grid only.
        assert not self.windows("media-browser-viewer")
        self.key(app.wid, "Return")
        assert not self.windows("media-browser-viewer"), "hidden marks still block activation"
        self.key(app.wid, "u", "Return")
        viewer = wait(lambda: self.windows("media-browser-viewer"), "filtered original opens")[0]
        wait(
            lambda: any(
                e.get("type") == "viewer" and e.get("path") == str(target) for e in app.events()
            ),
            "filtered original mapping",
        )
        self.key(viewer, "q")
        wait(lambda: not self.windows("media-browser-viewer"), "filtered photo close")
        app.cursor(target_id)
        self.key(app.wid, "slash", "ctrl+a")
        self.cmd("xdotool", "type", "--clearmodifiers", "--delay", "0", "jkq_no_match")
        wait(
            lambda: (
                app.state().get("query") == "jkq_no_match" and app.state()["native"]["total"] == 0
            ),
            "query typing does not navigate or quit",
        )
        self.key(app.wid, "Escape", "h", "j", "G", "0", "dollar", "f")
        assert app.p.poll() is None
        self.key(app.wid, "slash", "ctrl+a")
        self.cmd("xdotool", "type", "--clearmodifiers", "--delay", "0", "b/same")
        same_id = app.paths().index(folder / "b" / "same.png")
        wait(
            lambda: app.state().get("visible") == [same_id] and app.state()["native"]["total"] == 1,
            "duplicate basenames filter by folder",
        )
        self.key(app.wid, "ctrl+a")
        self.cmd("xdotool", "type", "--clearmodifiers", "--delay", "0", "2024")
        wait(
            lambda: (
                app.state().get("visible") == [target_id]
                and app.state()["native"]["id"] == target_id
            ),
            "folder/year query",
        )
        self.key(app.wid, "Return", "d")
        self.key(app.dialog(), "y")
        wait(lambda: not target.exists(), "filtered delete uses the original")
        wait(lambda: app.state()["native"]["total"] == 0, "last filtered result disappears")
        assert (folder / "b" / "same.png").exists()
        self.key(app.wid, "slash", "ctrl+a", "BackSpace")
        wait(
            lambda: (
                app.state().get("query") == ""
                and app.state()["native"]["total"] == len(app.paths()) - 1
            ),
            "clear restores remaining library",
        )
        self.key(app.wid, "Return")
        app.quit()

    def clipboard_paste(self):
        folder = self.home / "clipboard one's α"
        folder.mkdir()
        original = Image.new("RGBA", (1400, 900), (20, 100, 180, 127))
        original.paste((200, 80, 40, 255), (0, 0, 700, 450))
        original.save(folder / "image0.png")
        second = Image.new("RGB", (160, 96), (40, 170, 80))
        exif = Image.Exif()
        exif[274] = 6
        second.save(folder / "image1.jpg", exif=exif)
        self.video(folder / "image2.mp4", seconds=2)
        app = self.start(folder)
        self.key(app.wid, "v", "y")
        app.marks({0})

        def copied(path, mode):
            state = app.ini("clipboard-state.ini")
            return (
                state
                and state["clipboard"]["path"] == str(path)
                and state["clipboard"]["mode"] == mode
            )

        wait(lambda: copied(folder / "image0.png", "image"), "original image copied")
        result = json.loads(
            self.cmd(
                sys.executable,
                str(Path(__file__).with_name("clipboard.py")),
                "image",
                str(self.out / "clipboard-original.png"),
            )
        )
        assert (result["width"], result["height"]) == original.size
        assert "image/png" in result["targets"]
        with Image.open(self.out / "clipboard-original.png") as image:
            assert image.convert("RGBA").getpixel((1000, 600)) == original.getpixel((1000, 600))

        # Paste through Chromium's real X11 clipboard path into a local page.
        # No account, network media or external service is involved.
        suite = self
        self.paste_result = None
        self.paste_ready = False

        class PasteHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = Path(__file__).with_name("paste.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                assert length < 16 * 1024 * 1024
                value = json.loads(self.rfile.read(length))
                if self.path == "/ready":
                    suite.paste_ready = True
                else:
                    suite.paste_result = value
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.paste_server = ThreadingHTTPServer(("127.0.0.1", 0), PasteHandler)
        self.paste_thread = threading.Thread(target=self.paste_server.serve_forever, daemon=True)
        self.paste_thread.start()
        browser = self.spawn(
            [
                "chromium",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-background-networking",
                "--class=media-browser-paste-test",
                f"--user-data-dir={self.home / 'browser'}",
                f"--app=http://127.0.0.1:{self.paste_server.server_port}/",
            ],
            "browser-paste",
        )
        browser_window = wait(
            lambda: self.windows("media-browser-paste-test"), "real browser paste window"
        )[0]
        wait(lambda: self.paste_ready, "browser paste listener readiness")
        self.key(browser_window, "ctrl+v")
        pasted = wait(lambda: self.paste_result, "browser image paste", timeout=30)
        assert "error" not in pasted, pasted
        assert (pasted["type"], pasted["width"], pasted["height"]) == ("image/png", 1400, 900)
        raw = base64.b64decode(pasted.pop("data").split(",", 1)[1], validate=True)
        (self.out / "browser-pasted.png").write_bytes(raw)
        (self.out / "browser-paste.json").write_text(json.dumps(pasted, indent=2))
        with Image.open(self.out / "browser-pasted.png") as image:
            assert all(
                abs(a - b) <= 2
                for a, b in zip(
                    image.convert("RGBA").getpixel((1000, 600)), original.getpixel((1000, 600))
                )
            )
        self.shot(browser_window, "browser-paste")
        os.killpg(browser.pid, signal.SIGTERM)
        browser.wait(timeout=10)
        self.key(app.wid, "Y")
        wait(lambda: copied(folder / "image0.png", "path"), "original full path copied")
        assert json.loads(
            self.cmd(sys.executable, str(Path(__file__).with_name("clipboard.py")), "text")
        )["text"] == str(folder / "image0.png")
        app.marks({0})
        generation = app.ini("clipboard-state.ini")["clipboard"].getint("generation")
        self.key(app.wid, "y", "Y", "y")
        wait(
            lambda: (
                copied(folder / "image0.png", "image")
                and app.ini("clipboard-state.ini")["clipboard"].getint("generation")
                >= generation + 3
            ),
            "latest rapid clipboard request wins",
        )
        self.key(app.wid, "u", "Return")
        viewer = wait(lambda: self.windows("media-browser-viewer"), "clipboard photo viewer")[0]

        def photo_path():
            try:
                state = json.loads((app.session() / "photo-state.json").read_text())
                return base64.b64decode(state["path_base64"]).decode()
            except (OSError, json.JSONDecodeError):
                return None

        wait(lambda: photo_path() == str(folder / "image0.png"), "first photo loaded")
        self.key(viewer, "n")
        wait(lambda: photo_path() == str(folder / "image1.jpg"), "photo next navigation complete")
        self.key(viewer, "y")
        wait(lambda: copied(folder / "image1.jpg", "image"), "photo's browsed original copied")
        self.key(viewer, "Y")
        wait(lambda: copied(folder / "image1.jpg", "path"), "photo's browsed original path")
        assert json.loads(
            self.cmd(sys.executable, str(Path(__file__).with_name("clipboard.py")), "text")
        )["text"] == str(folder / "image1.jpg")
        self.key(viewer, "y")
        wait(lambda: copied(folder / "image1.jpg", "image"), "image ownership restored to gallery")
        self.key(viewer, "q")
        wait(lambda: not self.windows("media-browser-viewer"), "copied photo close")
        result = json.loads(
            self.cmd(
                sys.executable,
                str(Path(__file__).with_name("clipboard.py")),
                "image",
                str(self.out / "clipboard-oriented.png"),
            )
        )
        assert (result["width"], result["height"]) == (96, 160), (
            "full oriented original must survive viewer close"
        )
        app.cursor(0)
        app.keys_cursor(2, "G")
        self.key(app.wid, "y")
        error_window = wait(
            lambda: self.windows(title="Media Browser — Copy failed"),
            "video cannot be copied as a poster",
        )[0]
        self.key(error_window, "Escape")
        self.key(app.wid, "Y")
        wait(lambda: copied(folder / "image2.mp4", "path"), "video original path")
        assert json.loads(
            self.cmd(sys.executable, str(Path(__file__).with_name("clipboard.py")), "text")
        )["text"] == str(folder / "image2.mp4")
        app.quit()

    def navigation_selection(self):
        folder = self.home / "navigation one's"
        for i in range(101):
            self.photo(folder / f"image{i}.png")
        app = self.start(folder)
        self.tiled(app.wid)
        wait(
            lambda: len(app.posters()) >= min(101, app.state()["native"]["count"] * 2),
            "navigation cache",
        )
        self.shot(app.wid, "initial-gallery")
        app.goto(0)
        app.keys_cursor(1, "l")
        app.keys_cursor(0, "h")
        columns = app.state()["native"]["columns"]
        rows = app.state()["native"]["rows"]
        app.keys_cursor(columns, "j")
        app.keys_cursor(0, "k")
        app.keys_cursor(1, "Right")
        app.keys_cursor(0, "Left")
        app.keys_cursor(columns, "Down")
        app.keys_cursor(0, "Up")
        app.keys_cursor(min(100, columns * rows), "ctrl+d")
        app.keys_cursor(0, "ctrl+u")
        app.keys_cursor(min(100, columns * rows), "Next")
        app.keys_cursor(0, "Prior")
        app.keys_cursor(100, "End")
        app.keys_cursor(0, "Home")
        app.keys_cursor(100, "G")
        app.keys_cursor(100 - 100 % columns, "0")
        app.keys_cursor(100, "dollar")
        app.keys_cursor(0, "g", "g")
        # Native quick labels are observed, never reimplemented in the controller.
        self.key(app.wid, "f")
        self.shot(app.wid, "quick-labels")
        app.keys_cursor(1, "s")
        self.tiled(app.wid)
        self.key(app.wid, "f", "Escape")
        app.keys_cursor(2, "l")
        self.key(app.wid, "f")
        # Native follow timeout is five seconds; wait for recovery by actual key effect.
        time.sleep(5.2)
        app.keys_cursor(3, "l")
        for expected in (192, 128, 96, 64, 64):
            self.key(app.wid, "z", "o")
            wait(lambda: app.state()["native"]["size"] == expected, f"size {expected}")
        for expected in (96, 128, 192, 256, 384, 384):
            self.key(app.wid, "z", "i")
            wait(lambda: app.state()["native"]["size"] == expected, f"size {expected}")
        # Force a multi-character label at a dense viewport, including non-first target.
        for _ in range(5):
            self.key(app.wid, "z", "o")
        wait(lambda: app.state()["native"]["size"] == 64, "dense grid")
        app.goto(0)
        count = min(101, app.state()["native"]["count"])
        labels = quick_labels(count)
        for oracle_count in (1, 9, 10, 35, 81, 100):
            oracle = quick_labels(oracle_count)
            assert set(oracle.values()) == set(range(oracle_count))
            assert not any(a != b and b.startswith(a) for a in oracle for b in oracle)
        assert quick_labels(35)["ha"] == 5
        label, target = next((k, v) for k, v in labels.items() if len(k) > 1 and v > 0)
        self.key(app.wid, "f", *label)
        app.cursor(target)
        app.goto(2)
        self.key(app.wid, "v")
        app.keys_cursor(5, "l", "l", "l")
        app.marks({2, 3, 4, 5})
        app.keys_cursor(3, "h", "h")
        app.marks({2, 3})
        self.key(app.wid, "Escape")
        app.goto(7)
        self.key(app.wid, "v", "l", "l")
        app.marks({2, 3, 7, 8, 9})
        self.key(app.wid, "h")
        app.marks({2, 3, 7, 8})
        self.key(app.wid, "Escape", "Return", "Return")
        assert not self.windows("media-browser-viewer") and not self.windows("mpv")
        self.key(app.wid, "d")
        dialog = app.dialog()
        self.key(dialog, "n")
        wait(lambda: not self.windows(title="Move to Trash?"), "dialog cancellation")
        app.marks({2, 3, 7, 8})
        self.key(app.wid, "Return")
        assert not self.windows("media-browser-viewer")
        self.key(app.wid, "u")
        app.marks(set())
        for _ in range(4):
            app.goto(0)
            self.key(app.wid, "v", "l", "l", "Escape", "Return", "u")
            app.marks(set())
        assert not self.windows("media-browser-viewer")
        # Use actual page-sized geometry while extending and shrinking a range.
        for _ in range(4):
            self.key(app.wid, "z", "i")
        wait(lambda: app.state()["native"]["size"] == 256, "selection viewport size")
        app.goto(0)
        self.key(app.wid, "v", "Down")
        c = app.state()["native"]["columns"]
        app.cursor(c)
        app.marks(set(range(c + 1)))
        self.key(app.wid, "Up")
        app.marks({0})
        self.key(app.wid, "Next")
        wait(lambda: len(app.state()["marked"]) > c, "selection across viewport")
        self.key(app.wid, "Escape", "u")
        app.marks(set())
        app.goto(0)
        self.key(app.wid, "v", "G")
        app.marks(set(range(101)))
        self.key(app.wid, "0")
        app.marks(set(range(101 - 100 % c)))
        self.key(app.wid, "dollar")
        app.marks(set(range(101)))
        self.key(app.wid, "g", "g")
        app.marks({0})
        self.key(app.wid, "Escape", "u")
        app.marks(set())
        # Real click synchronization against the native grid geometry.
        app.goto(0)
        g = self.image(app.wid)
        cell = app.state()["native"]["size"] + 10
        x = app.state()["native"].get("origin_x", 0) + g.width % cell // 2 + cell + cell // 2
        y = app.state()["native"].get("origin_y", 0) + g.height % cell // 2 + cell // 2
        self.cmd("xdotool", "mousemove", "--window", str(app.wid), str(x), str(y), "click", "1")
        app.cursor(1)
        self.key(app.wid, "Return")
        viewer = wait(lambda: self.windows("media-browser-viewer"), "photo open after clear")[0]
        self.key(viewer, "q")
        wait(lambda: not self.windows("media-browser-viewer"), "photo close")
        assert app.p.poll() is None and app.wid in self.windows()
        # Double-click is the native supported mouse activation path.
        self.focus(app.wid)
        self.cmd(
            "xdotool",
            "mousemove",
            "--window",
            str(app.wid),
            str(x),
            str(y),
            "click",
            "--repeat",
            "2",
            "--delay",
            "100",
            "1",
        )
        viewer = wait(lambda: self.windows("media-browser-viewer"), "double click original")[0]
        self.key(viewer, "q")
        wait(lambda: not self.windows("media-browser-viewer"), "double-click viewer close")
        app.quit()

    def fit_lifecycle(self):
        folder = self.home / "fit"
        self.photo(folder / "small.png", (120, 80))
        self.photo(folder / "large.png", (3000, 2000), (220, 150, 30))
        app = self.start(folder)
        for index in (0, 1):
            app.goto(index)
            self.key(app.wid, "Return")
            viewer = wait(lambda: self.windows("media-browser-viewer"), "fit viewer")[0]
            self.tiled(viewer)
            expected = (220, 150, 30) if index == 0 else (30, 150, 220)  # natural large, small

            def fitted():
                image = self.image(viewer)
                pixels = image.load()
                points = [
                    (x, y)
                    for y in range(0, image.height, 4)
                    for x in range(0, image.width, 4)
                    if all(abs(pixels[x, y][i] - expected[i]) <= 3 for i in range(3))
                ]
                if not points:
                    return False
                width = max(p[0] for p in points) - min(p[0] for p in points) + 4
                height = max(p[1] for p in points) - min(p[1] for p in points) + 4
                ideal = min(image.width / 3, image.height / 2)
                return abs(width - 3 * ideal) < 20 and abs(height - 2 * ideal) < 20

            wait(fitted, "aspect-correct first-open fit")
            self.shot(viewer, f"initial-fit-{index}")
            state_path = app.session() / "photo-state.json"

            def state():
                try:
                    return json.loads(state_path.read_text())
                except (OSError, json.JSONDecodeError):
                    return None

            before = wait(state, "photo diagnostic state")
            self.key(viewer, "z", "i")
            wait(lambda: state()["scale"] > before["scale"], "actual photo zoom in")
            zoomed = state()["scale"]
            for key, prop, delta in [
                ("h", "shift_x", 40),
                ("j", "shift_y", -40),
                ("k", "shift_y", 40),
                ("l", "shift_x", -40),
            ]:
                old = state()[prop]
                self.key(viewer, key)
                wait(lambda: state()[prop] == old + delta, f"actual photo pan {key}")
            self.key(viewer, "z", "o")
            wait(lambda: state()["scale"] < zoomed, "actual photo zoom out")
            original = state()["path_base64"]
            self.key(viewer, "n")
            wait(lambda: state()["path_base64"] != original, "actual sibling next")
            self.key(viewer, "p")
            wait(lambda: state()["path_base64"] == original, "actual sibling previous")
            self.key(viewer, "f")
            wait(lambda: ",false,true" in self.wm_state(viewer), "photo fullscreen")
            self.focus(app.wid)
            self.focus(viewer)
            self.key(viewer, "f")
            wait(lambda: "false,false,false" in self.wm_state(viewer), "photo normal")
            self.key(viewer, "Escape" if index == 0 else "Tab")
            wait(lambda: not self.windows("media-browser-viewer"), "viewer-only close")
            assert app.p.poll() is None and app.wid in self.windows()
            app.cursor(index)
        app.quit()

    def first_fit_fast_format(self):
        # Each viewer must start with a fast-decoding still as its first/only
        # file. A preceding slow PNG/AVIF can hide discarded startup actions.
        for extension in ("ppm", "bmp"):
            folder = self.home / ("first-fit-fast-" + extension)
            self.photo(folder / ("only." + extension), (120, 80))
            app = self.start(folder)
            self.key(app.wid, "Return")
            viewer = wait(lambda: self.windows("media-browser-viewer"), "fast-format viewer")[0]
            self.tiled(viewer)

            def fitted():
                image = self.image(viewer)
                points = [
                    (x, y)
                    for y in range(0, image.height, 4)
                    for x in range(0, image.width, 4)
                    if all(abs(a - b) < 4 for a, b in zip(image.getpixel((x, y)), (30, 150, 220)))
                ]
                if not points:
                    return False
                width = max(p[0] for p in points) - min(p[0] for p in points) + 4
                height = max(p[1] for p in points) - min(p[1] for p in points) + 4
                scale = min(image.width / 3, image.height / 2)
                return abs(width - 3 * scale) < 20 and abs(height - 2 * scale) < 20

            wait(fitted, "fast-format first-open allocated-tile fit")
            native = json.loads((app.session() / "photo-state.json").read_text())
            assert native["scale_mode"] == 4 and native["scale"] > 1
            self.shot(viewer, "first-fit-fast-" + extension)
            self.key(viewer, "q")
            wait(lambda: not self.windows("media-browser-viewer"), "fast-format viewer close")
            app.quit()

    def confirmation_trash(self):
        folder = self.home / "trash original one's"
        for i in range(12):
            self.photo(folder / f"image{i}.png", color=(i * 15, 120, 210))
        originals = {p: p.read_bytes() for p in folder.iterdir()}
        app = self.start(folder)
        for count, cancel in [(1, "Return"), (5, "n"), (6, "Escape"), (8, None)]:
            self.key(app.wid, "u")
            app.goto(0)
            if count > 1:
                self.key(app.wid, "v", *("l" for _ in range(count - 1)), "Escape")
                app.marks(set(range(count)))
            self.key(app.wid, "d")
            dialog = app.dialog()
            text = wait(
                lambda: (
                    (app.session() / "confirm.bin.shown").read_text()
                    if (app.session() / "confirm.bin.shown").exists()
                    else None
                ),
                "actual GTK dialog properties",
            )
            assert ("Move this file" if count == 1 else f"Move {count} files") in text
            for p in app.paths()[:count]:
                assert (str(p) in text) == (count <= 5), text
            self.shot(dialog, f"confirmation-{count}")
            if cancel:
                self.key(dialog, cancel)
            else:
                result = self.cmd(
                    "awesome-client",
                    f"for _,c in ipairs(client.get()) do if c.window == {dialog} then c:kill() end end",
                )
                assert "error" not in result.lower(), result
            wait(lambda: not self.windows(title="Move to Trash?"), "cancelled dialog")
            assert all(p.exists() for p in originals)
            if count > 1:
                app.marks(set(range(count)))
        self.key(app.wid, "u")
        app.goto(1)
        self.key(app.wid, "v", "l", "Escape")
        app.goto(7)
        self.key(app.wid, "v", "l", "Escape")
        app.marks({1, 2, 7, 8})
        selected = [app.paths()[i] for i in (1, 2, 7, 8)]
        self.key(app.wid, "d")
        self.key(app.dialog(), "y")
        wait(lambda: all(not p.exists() for p in selected), "confirmed originals trashed")
        app.marks(set())
        assert all(p.exists() for p in originals if p not in selected)
        trash = Path(self.env["XDG_DATA_HOME"]) / "Trash"
        for p in selected:
            assert (trash / "files" / p.name).read_bytes() == originals[p]
            info = (trash / "info" / (p.name + ".trashinfo")).read_text()
            assert f"Path={p}" in unquote(info) and "DeletionDate=" in info
        self.key(app.wid, "Return")
        viewer = wait(lambda: self.windows("media-browser-viewer"), "open after successful trash")[
            0
        ]
        expected_id = app.state()["cursor"]
        expected_path = app.paths()[expected_id]
        wait(
            lambda: any(
                e.get("type") == "viewer" and e.get("path") == str(expected_path)
                for e in app.events()
            ),
            "post-delete original mapping",
        )
        self.key(viewer, "q")
        app.quit()
        # Repeat the former last-file GTK/Cairo race using fresh real galleries.
        for i in range(4):
            only = self.home / f"last-{i}" / "only.png"
            self.photo(only)
            app = self.start(only.parent)
            self.key(app.wid, "f")  # A single-tile quick jump must not divide by zero.
            assert app.p.poll() is None
            self.key(app.wid, "d")
            self.key(app.dialog(), "y")
            wait(lambda: not only.exists(), "last original trashed")
            wait(lambda: app.state()["native"]["total"] == 0, "empty gallery state")
            assert app.p.poll() is None and app.wid in self.windows()
            app.quit()

    def partial_failure(self):
        folder = self.home / "partial-failure"
        self.photo(folder / "a-writable" / "ok.png")
        self.photo(folder / "b-readonly" / "fail.png")
        app = self.start(folder)
        self.key(app.wid, "v", "l", "Escape")
        app.marks({0, 1})
        self.key(app.wid, "d")
        dialog = app.dialog()
        readonly = folder / "b-readonly"
        readonly.chmod(0o555)
        try:
            self.key(dialog, "y")
            error = wait(lambda: self.windows(title="Trash failed"), "real partial-error dialog")[0]
            assert not (folder / "a-writable/ok.png").exists()
            assert (readonly / "fail.png").exists(), "failure must never permanently delete"
            app.marks({1})
            self.shot(error, "partial-trash-error")
            self.key(error, "Return")
            wait(lambda: not self.windows(title="Trash failed"), "error close")
            self.key(app.wid, "Return")
            assert not self.windows("media-browser-viewer"), "failed mark must block Enter"
            self.key(app.wid, "u", "Return")
            viewer = wait(
                lambda: self.windows("media-browser-viewer"), "failed original remains viewable"
            )[0]
            self.key(viewer, "q")
            app.quit()
            app = self.start(folder)
            self.key(app.wid, "d")
            self.key(app.dialog(), "y")
            wait(lambda: self.windows(title="Trash failed"), "pending error UI")
            app.p.terminate()
            app.p.wait(timeout=20)
            wait(
                lambda: (
                    not self.windows("media-browser-gallery")
                    and not self.windows(title="Trash failed")
                ),
                "shutdown during error handling",
            )
            app.reaped()
        finally:
            readonly.chmod(0o755)

    def image_formats_orientation(self):
        folder = self.home / "formats"
        folder.mkdir()
        for extension in ("png", "jpg", "gif", "bmp", "tiff", "webp", "ico", "avif"):
            image = Image.new("RGB", (128, 80), (25, 155, 225))
            if extension == "ico":
                image = image.convert("RGBA")  # PNG-backed ICO requires RGBA.
            image.save(folder / f"image.{extension}")
        (folder / "image.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="80"><rect width="128" height="80" fill="#199be1"/></svg>'
        )
        original = Image.new("RGB", (160, 80), (30, 150, 220))
        exif = Image.Exif()
        exif[274] = 6
        original.save(folder / "orientation.jpg", exif=exif)
        app = self.start(folder)
        wait(
            lambda: len(app.posters()) >= min(len(app.paths()), app.state()["native"]["count"] * 2),
            "format cache",
        )
        for i, path in enumerate(app.paths()):
            app.goto(i)
            wait(lambda: app.poster(path).exists(), f"poster for {path.suffix}")
            self.key(app.wid, "Return")
            viewer = wait(
                lambda: self.windows("media-browser-viewer"), f"private backend {path.suffix}"
            )[0]
            image = wait(
                lambda: (
                    self.image(viewer)
                    if self.image(viewer).getpixel(
                        (self.image(viewer).width // 2, self.image(viewer).height // 2)
                    )[2]
                    > 150
                    else None
                ),
                f"decoded image {path.suffix}",
            )
            assert image.width > 0
            self.key(viewer, "q")
            wait(lambda: not self.windows("media-browser-viewer"), "format viewer close")
            if path.name == "orientation.jpg":
                with Image.open(app.poster(path)) as poster:
                    assert poster.height == 2 * poster.width, (
                        "EXIF orientation must preserve aspect"
                    )
        app.quit()

    def legacy_cache(self):
        folder = self.home / "legacy"
        folder.mkdir()
        path = folder / "old one's.mp4"
        path.write_bytes(b"synthetic video: decoding this would fail")
        root = Path(self.env["XDG_CACHE_HOME"]) / "media-browser/thumbnails/posters"
        root.mkdir(parents=True, exist_ok=True)
        target = root / (
            hashlib.md5(("file://" + str(path)).encode(), usedforsecurity=False).hexdigest()
            + ".png"
        )
        stat = path.stat()
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 512, 288)
        pixbuf.fill(0x406080FF)
        metadata = {
            "Thumb::URI": "file://" + str(path),
            "Thumb::MTime": str(int(stat.st_mtime)),
            "Thumb::MTimeNS": str(stat.st_mtime_ns),
            "Thumb::CTimeNS": str(stat.st_ctime_ns),
            "Thumb::Device": str(stat.st_dev),
            "Thumb::Inode": str(stat.st_ino),
            "Thumb::Size": str(stat.st_size),
        }
        pixbuf.savev(str(target), "png", ["tEXt::" + k for k in metadata], list(metadata.values()))
        app = self.start(folder)
        wait(
            lambda: any(e.get("outcome") == "Upgraded" for e in app.events()),
            "legacy upgrade without a decodable video",
        )
        assert not any(e.get("outcome") == "Decoded" for e in app.events())
        stamp = target.stat().st_mtime_ns
        app.quit()
        app = self.start(folder)
        wait(lambda: any(e.get("outcome") == "Reused" for e in app.events()), "badged legacy reuse")
        assert target.stat().st_mtime_ns == stamp
        app.quit()

    def mpv(self, command):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(str(self.mpv_socket))
            s.sendall(json.dumps({"command": command, "request_id": 73}).encode() + b"\n")
            f = s.makefile("rb")
            for line in f:
                response = json.loads(line)
                if response.get("request_id") == 73:
                    assert response["error"] == "success", response
                    with open(self.out / "mpv-ipc.jsonl", "a") as out:
                        out.write(json.dumps({"command": command, "response": response}) + "\n")
                    return response.get("data")
        raise AssertionError("mpv did not reply")

    def video_playback(self):
        folder = self.home / "videos"
        folder.mkdir()
        self.video(folder / "h264.mp4")
        self.video(folder / "hevc.mp4", "hevc", 2)
        self.video(folder / "vp9.webm", "vp9", 2)
        self.photo(folder / "photo.png")
        app = self.start(folder)
        wait(lambda: len(app.posters()) == 4, "all codec posters")
        for source in app.catalog()["sources"]:
            p = GdkPixbuf.Pixbuf.new_from_file(str(app.poster(Path(source["path"]))))
            badge = p.get_option("tEXt::Thumb::MediaBrowserVideoBadge")
            assert (badge == "play-v1") == source["video"]
            if source["video"]:
                image = Image.open(app.poster(Path(source["path"])))
                assert (
                    len(
                        image.crop((0, 0, image.width // 2, image.height // 2)).getcolors(1_000_000)
                    )
                    > 20
                )
        self.shot(app.wid, "video-badges")
        # Compare actual rendered badge pixels to the scaled cached master.
        # Keep the selection border out of the comparison rectangle.
        original = Image.open(app.poster(folder / "h264.mp4")).convert("RGB")
        size = app.state()["native"]["size"]
        factor = min(size / original.width, size / original.height, 1)
        expected = original.resize(
            (round(original.width * factor), round(original.height * factor)),
            Image.Resampling.BILINEAR,
        )

        def rendered_badge():
            rendered = self.image(app.wid)
            cell = size + 10
            x = rendered.width % cell // 2 + (cell - expected.width) // 2
            y = rendered.height % cell // 2 + (cell - expected.height) // 2
            region = (
                expected.width - 45,
                expected.height - 45,
                expected.width - 8,
                expected.height - 8,
            )
            actual = rendered.crop((x + region[0], y + region[1], x + region[2], y + region[3]))
            error = ImageStat.Stat(ImageChops.difference(actual, expected.crop(region))).mean
            return max(error) < 12

        wait(rendered_badge, "badge survives actual gallery rendering")
        for _ in range(2):
            app.goto(0)
            self.key(app.wid, "Return")
            wait(lambda: self.windows("mpv"), "real mpv")
            wait(lambda: self.mpv_socket.exists(), "mpv IPC")
            assert self.mpv(["get_property", "path"]) == str(folder / "h264.mp4")
            wait(lambda: self.mpv(["get_property", "time-pos"]) > 0.1, "advancing video")
            assert self.mpv(["get_property", "audio-params"])["samplerate"] == 48000
            assert self.mpv(["get_property", "video-params"])["w"] == 160
            self.mpv(["quit"])
            wait(lambda: not self.windows("mpv"), "video close")
            assert app.p.poll() is None and app.wid in self.windows()
        app.quit()

    def gif_playback(self):
        folder = self.home / "GIF animation one's α"
        folder.mkdir()
        palette = [0, 0, 0, 220, 60, 40, 40, 80, 220]
        palette += [
            value
            for i in range(3, 256)
            for value in ((i * 7) % 256, (i * 13) % 256, (i * 19) % 256)
        ]
        frames = []
        for color in (0, 1, 2):
            frame = Image.new("P", (200, 120), color)
            frame.putpalette(palette)
            if color:
                for y in range(30):
                    for x in range(200):
                        frame.putpixel((x, y), 3 + (x + y) % 253)
            frames.append(frame)
        animated = folder / "animation one's.gif"
        frames[0].save(
            animated,
            save_all=True,
            append_images=frames[1:],
            duration=[400, 250, 250],
            loop=0,
            transparency=0,
            disposal=2,
            optimize=False,
        )
        extensionless = folder / "animation-no-extension"
        uppercase = folder / "animation-upper.GIF"
        shutil.copyfile(animated, extensionless)
        shutil.copyfile(animated, uppercase)
        still = folder / "still.gif"
        frames[2].save(still, optimize=False)

        # Recreate a valid old cache containing only the transparent first
        # frame. A badge-only upgrade must not preserve this empty poster.
        cache = Path(self.env["XDG_CACHE_HOME"]) / "media-browser/thumbnails/posters"
        cache.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5(
            ("file://" + str(animated)).encode(), usedforsecurity=False
        ).hexdigest()
        stat = animated.stat()
        blank = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 512, 307)
        blank.fill(0)
        metadata = {
            "Thumb::URI": "file://" + str(animated),
            "Thumb::MTime": str(int(stat.st_mtime)),
            "Thumb::MTimeNS": str(stat.st_mtime_ns),
            "Thumb::CTimeNS": str(stat.st_ctime_ns),
            "Thumb::Device": str(stat.st_dev),
            "Thumb::Inode": str(stat.st_ino),
            "Thumb::Size": str(stat.st_size),
        }
        blank.savev(
            str(cache / (digest + ".png")),
            "png",
            ["tEXt::" + key for key in metadata],
            list(metadata.values()),
        )
        app = self.start(folder)
        wait(lambda: len(app.posters()) == 4, "GIF posters")
        for path in (animated, extensionless, uppercase):
            with Image.open(app.poster(path)) as image:
                center = image.convert("RGBA").crop(
                    (
                        image.width // 4,
                        image.height // 4,
                        image.width * 3 // 4,
                        image.height * 3 // 4,
                    )
                )
                assert center.getchannel("A").getextrema()[0] > 200, (
                    "GIF poster must contain a visible decoded frame, not an empty first frame/badge"
                )
                assert sum(ImageStat.Stat(center.convert("RGB")).mean) > 100
                assert image.info.get("Thumb::MediaBrowserGifBadge") == "dots-v1"
                assert image.info.get("Thumb::MediaBrowserVideoBadge", "") == ""
            source = next(s for s in app.catalog()["sources"] if s["path"] == str(path))
            assert source["video"], "animated GIF must be dispatched to mpv"
        assert not next(s for s in app.catalog()["sources"] if s["path"] == str(still))["video"]
        with Image.open(app.poster(still)) as image:
            assert image.info.get("Thumb::MediaBrowserGifBadge") == "dots-v1"
        self.shot(app.wid, "gif-posters")
        stamps = {path: app.poster(path).stat().st_mtime_ns for path in app.paths()}
        for path in (animated, extensionless, uppercase):
            app.goto(app.paths().index(path))
            self.key(app.wid, "Return")
            window = wait(lambda: self.windows("mpv"), "GIF playing in real mpv")[0]
            assert not self.windows("media-browser-viewer")
            wait(lambda: self.mpv_socket.exists(), "GIF mpv IPC")
            assert self.mpv(["get_property", "path"]) == str(path)
            wait(lambda: self.mpv(["get_property", "time-pos"]) > 0.1, "GIF playback advances")
            seen = set()

            def moving():
                image = self.image(window)
                red, _, blue = image.getpixel((image.width // 2, image.height // 2))
                if red > 140 and red > blue * 1.4:
                    seen.add("red")
                if blue > 140 and blue > red * 1.4:
                    seen.add("blue")
                return len(seen) == 2

            wait(moving, "GIF must actually animate through colored frames", timeout=15)
            self.mpv(["quit"])
            wait(lambda: not self.windows("mpv"), "GIF viewer close")
            assert app.p.poll() is None and app.wid in self.windows()
        app.goto(app.paths().index(still))
        self.key(app.wid, "Return")
        viewer = wait(
            lambda: self.windows("media-browser-viewer"), "single-frame GIF stays a photo"
        )[0]
        self.key(viewer, "q")
        app.quit()
        app = self.start(folder)
        wait(
            lambda: len([e for e in app.events() if e.get("type") == "poster"]) == 4,
            "warm GIF poster reuse",
        )
        assert not any(e.get("outcome") == "Decoded" for e in app.events())
        assert all(app.poster(path).stat().st_mtime_ns == stamp for path, stamp in stamps.items())
        app.quit()

    def cache_progressive(self):
        folder = self.home / "501 mixed"
        for i in range(500):
            self.photo(folder / f"image{i}.jpg", (2400 + i % 3 * 600, 1600), (30, 150, 220))
        self.video(folder / "image500.mp4", seconds=2)
        app = self.start(folder)
        count = min(501, app.state()["native"]["count"])
        wait(lambda: app.poster(app.paths()[0]).exists(), "first visible poster")

        # Sample the same rendered, already populated tile while other entries
        # complete. XGetImage is independent of app/cache diagnostics.
        def populated_pixel():
            image = self.image(app.wid)
            cell = app.state()["native"]["size"] + 10
            return image.getpixel(
                (image.width % cell // 2 + cell // 2, image.height % cell // 2 + cell // 2)
            )

        def first_tile_loaded():
            pixel = populated_pixel()
            # Theme accents may also be blue. Match the original's pixels,
            # never a loading cursor or themed background.
            return all(abs(a - b) <= 4 for a, b in zip(pixel, (30, 150, 220)))

        wait(first_tile_loaded, "first tile rendered")
        samples = [populated_pixel() for _ in range(525)]
        (self.out / "flicker-samples.json").write_text(json.dumps(samples))
        assert all(all(abs(a - b) <= 4 for a, b in zip(p, (30, 150, 220))) for p in samples), (
            "populated tile regressed to placeholder"
        )
        wait(
            lambda: len(app.posters()) >= min(501, count * 2),
            "initial visible plus adjacent cache",
            timeout=90,
        )
        assert len(app.posters()) == min(501, count * 2), (len(app.posters()), count)
        assert all(
            e["work"]["active"] <= 2 and len(e["work"]["wanted"]) <= 2 * count
            for e in app.events()
            if "work" in e
        ), "bounded jobs"
        stamps = {p: p.stat().st_mtime_ns for p in app.posters()}
        self.key(app.wid, "Next")
        app.cursor(count)
        wait(lambda: len(app.posters()) >= min(501, count * 3), "new adjacent page", timeout=90)
        self.key(app.wid, "Prior")
        app.cursor(0)
        self.key(app.wid, "G")
        app.cursor(500)
        wait(lambda: app.poster(app.paths()[500]).exists(), "far viewport video", timeout=60)
        for i in range(count * 3, max(count * 3, 501 - count * 2)):
            assert not app.poster(app.paths()[i]).exists(), f"untouched middle {i} decoded"
        self.key(app.wid, "v", "h", "Escape", "z", "o", "z", "i")
        assert not self.windows("media-browser-viewer")
        app.quit()
        self._cache_progressive_warm(folder, count, stamps)

    def thumbnail_publication_race(self):
        folder = self.home / "thumbnail-publication"
        self.photo(folder / "image0.png", (320, 240), (30, 150, 220))
        self.photo(folder / "image1.png", (960, 1280), (210, 60, 30))
        user = Path(self.env["XDG_CONFIG_HOME"]) / "media-browser"
        user.mkdir(exist_ok=True)
        (user / "pqivrc").write_text("[keybindings]\n@MONTAGE { <F5> { reload_thumbnail(1); } }\n")
        app = self.start(folder)
        wait(lambda: len(app.posters()) == 2, "publication fixture cached")
        geometry = self.image(app.wid)
        cell = app.state()["native"]["size"] + 10

        def pixel(index):
            image = self.image(app.wid)
            return image.getpixel(
                (
                    geometry.width % cell // 2 + index * cell + cell // 2,
                    geometry.height % cell // 2 + cell // 2,
                )
            )

        wait(
            lambda: all(abs(a - b) < 4 for a, b in zip(pixel(1), (210, 60, 30))),
            "original portrait tile rendered",
        )
        session = app.session()
        entry = session / "entries/000000000001.png"
        old_placeholder = (session / "loading.png").read_bytes()
        writer = None
        stopped = False
        try:
            # Hold only the Rust controller. The real renderer remains live.
            # Its worker opens a FIFO at this stable catalog entry, allowing
            # publication to replace the pathname while the old read is active.
            app.p.send_signal(signal.SIGSTOP)
            stopped = True
            entry.unlink()
            os.mkfifo(entry)
            self.key(app.wid, "F5")

            def reader_opened():
                nonlocal writer
                try:
                    writer = os.open(entry, os.O_WRONLY | os.O_NONBLOCK)
                    return True
                except OSError as e:
                    import errno

                    if e.errno == errno.ENXIO:
                        return False
                    raise

            wait(reader_opened, "native thumbnail worker reading old catalog inode")
            self.photo(folder / "image1.png", (960, 1280), (30, 190, 80))
            app.p.send_signal(signal.SIGCONT)
            stopped = False
            wait(
                lambda: (
                    len([e for e in app.events() if e.get("type") == "poster" and e.get("id") == 1])
                    >= 2
                ),
                "replacement portrait published while native old read is active",
            )
            self.key(app.wid, "Y")
            wait(
                lambda: app.ini("clipboard-state.ini") is not None,
                "native control queue processed publication",
            )
            # Release the old transparent bytes only after reload_thumbnail has
            # been processed. This must never overwrite the newer portrait.
            os.set_blocking(writer, True)
            os.write(writer, old_placeholder)
            os.close(writer)
            writer = None
            wait(
                lambda: all(abs(a - b) < 4 for a, b in zip(pixel(1), (30, 190, 80))),
                "new portrait survives stale native thumbnail completion",
            )
            assert all(abs(a - b) < 4 for a, b in zip(pixel(0), (30, 150, 220))), (
                "unrelated loaded tile must remain populated"
            )
            self.shot(app.wid, "thumbnail-publication-race")
            app.quit()
        finally:
            if writer is not None:
                os.close(writer)
            if stopped:
                app.p.send_signal(signal.SIGCONT)
            (user / "pqivrc").unlink(missing_ok=True)

    def _cache_progressive_warm(self, folder, count, stamps):
        assert all(p.stat().st_mtime_ns == stamp for p, stamp in stamps.items())
        app = self.start(folder)
        wait(
            lambda: len([e for e in app.events() if e.get("type") == "poster"]) >= count,
            "warm restart publication",
        )
        assert all(p.stat().st_mtime_ns == stamp for p, stamp in stamps.items())
        assert not any(e.get("outcome") == "Decoded" for e in app.events())
        source = app.paths()[0]
        stamp = source.stat()
        self.photo(source, (2400, 1600), (220, 100, 30))
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        old = stamps[app.poster(source)]
        wait(lambda: app.poster(source).stat().st_mtime_ns != old, "same-mtime invalidation")
        poster = app.poster(app.paths()[1])
        poster.write_bytes(b"corrupt png")
        wait(lambda: poster.stat().st_size > 100, "corrupted cache regenerated")
        app.poster(app.paths()[2]).unlink()
        wait(lambda: app.poster(app.paths()[2]).exists(), "missing cache regenerated")
        # Keep an actual viewer available while warm gallery resize/mark work
        # changes its allocated tile and cache completions continue.
        self.key(app.wid, "u", "Return")
        viewer = wait(lambda: self.windows("media-browser-viewer"), "viewer during warm scrolling")[
            0
        ]

        # Wait for the viewer's first native draw in its allocated tile, rather
        # than typing during GTK's initial map/focus negotiation.
        def photo_ready():
            try:
                state = json.loads((app.session() / "photo-state.json").read_text())
                geometry = self.x.create_resource_object("window", viewer).get_geometry()
                return state["width"] == geometry.width and state["height"] == geometry.height
            except (OSError, json.JSONDecodeError):
                return False

        wait(photo_ready, "warm viewer allocated and rendered")
        self.key(app.wid, "v")
        app.marks({0})
        app.keys_cursor(1, "l")
        self.key(app.wid, "Escape", "z", "o", "z", "i")
        app.marks({0, 1})
        self.focus(viewer)
        assert int(self.cmd("xdotool", "getactivewindow")) == viewer
        self.key(viewer, "q")
        wait(lambda: not self.windows("media-browser-viewer"), "warm viewer clean close")
        self.key(app.wid, "u")
        app.marks(set())
        app.quit()

    def cache_mutations(self):
        folder = self.home / "mutations"
        for i in range(3):
            self.photo(folder / f"image{i}.png")
        app = self.start(folder)
        wait(lambda: len(app.posters()) == 3, "mutation cold cache")
        paths = app.paths()
        stamps = {p: app.poster(p).stat().st_mtime_ns for p in paths}
        self.photo(paths[0], color=(220, 100, 30))
        wait(
            lambda: app.poster(paths[0]).stat().st_mtime_ns != stamps[paths[0]],
            "normal content invalidation",
        )
        assert all(app.poster(p).stat().st_mtime_ns == stamps[p] for p in paths[1:])
        replacement = folder / "replacement"
        self.photo(replacement, color=(220, 100, 30))
        old_inode = paths[1].stat().st_ino
        os.replace(replacement, paths[1])
        assert paths[1].stat().st_ino != old_inode
        wait(
            lambda: app.poster(paths[1]).stat().st_mtime_ns != stamps[paths[1]],
            "same-path inode replacement",
        )
        assert app.poster(paths[2]).stat().st_mtime_ns == stamps[paths[2]]
        self.photo(folder / "new.png")
        app.quit()
        app = self.start(folder)
        assert len(app.paths()) == 4
        wait(lambda: len(app.posters()) == 4, "new source after discovery")
        wait(
            lambda: any(e.get("outcome") == "Decoded" for e in app.events()),
            "new source completion event",
        )
        changed = [e["id"] for e in app.events() if e.get("outcome") == "Decoded"]
        assert changed == [3], changed
        app.quit()

    def stale_generation(self):
        folder = self.home / "stale-video"
        folder.mkdir()
        self.video(folder / "image0.mp4", seconds=4, size="3840x2160")
        shutil.copyfile(folder / "image0.mp4", folder / "image1.mp4")
        app = self.start(folder)

        def decoding():
            result = []
            for directory in Path("/proc").iterdir():
                if not directory.name.isdigit():
                    continue
                try:
                    argv = (directory / "cmdline").read_bytes().split(b"\0")
                    if argv[0] == b"/usr/bin/ffmpeg" and any(
                        str(folder).encode() in a for a in argv
                    ):
                        result.append(int(directory.name))
                except (OSError, IndexError):
                    pass
            return result

        workers = wait(decoding, "actual in-flight video decoder", timeout=30)
        for pid in workers:
            os.kill(pid, signal.SIGSTOP)
        try:
            self.photo(folder / "replacement.png", color=(220, 100, 30))
            os.replace(folder / "replacement.png", folder / "image0.mp4")
            (folder / "image1.mp4").unlink()
        finally:
            for pid in workers:
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
        wait(
            lambda: app.poster(folder / "image0.mp4").exists(),
            "changed source eventually regenerated",
            timeout=90,
        )
        with Image.open(app.poster(folder / "image0.mp4")) as image:
            pixel = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
            assert all(abs(a - b) < 5 for a, b in zip(pixel, (220, 100, 30))), pixel
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(app.poster(folder / "image0.mp4")))
        assert pixbuf.get_option("tEXt::Thumb::CTimeNS") == str(
            (folder / "image0.mp4").stat().st_ctime_ns
        )
        wait(lambda: app.state()["native"]["total"] == 1, "source deleted during generation pruned")
        assert not app.poster(folder / "image1.mp4").exists(), (
            "deleted source completion must not publish"
        )
        app.quit()

    def cancellation(self):
        folder = self.home / "501 mixed"
        if not folder.exists():
            for i in range(8):
                self.photo(folder / f"image{i}.png")
        app = self.start(folder)
        self.key(app.wid, "G", "Home", "G")
        app.quit()
        app = self.start(folder)
        self.key(app.wid, "d")
        app.dialog()
        app.p.terminate()
        app.p.wait(timeout=20)
        wait(lambda: not self.windows("media-browser-gallery"), "quit during confirmation")
        assert not app.session().exists(), "private IPC/session cleanup"
        app.reaped()
        assert all(e["work"]["active"] <= 2 for a in self.apps for e in a.events() if "work" in e)
        fresh = self.home / "cancel-active"
        for i in range(8):
            self.photo(fresh / f"image{i}.jpg", (3500, 2500))
        app = self.start(fresh)
        wait(
            lambda: any(e.get("work", {}).get("active", 0) > 0 for e in app.events()),
            "actual active generation before quit",
        )
        app.quit()
        app = self.start(fresh)
        self.key(app.wid, "Return")
        app.p.terminate()
        assert app.p.wait(timeout=20) == 0
        wait(
            lambda: (
                not self.windows("media-browser-viewer")
                and not self.windows("media-browser-gallery")
            ),
            "quit during pending photo launch",
        )
        app.reaped()
        cold_video = self.home / "cancel-decoder"
        cold_video.mkdir()
        self.video(cold_video / "video.mp4", seconds=3, size="1920x1080")
        app = self.start(cold_video)
        wait(
            lambda: any(name in ("ffmpeg", "ffprobe") for _, name in app.members()),
            "real cancellable decoder process",
        )
        app.quit()


class App:
    def __init__(self, suite, p, trace):
        self.s, self.p, self.trace, self.wid = suite, p, trace, None

    def events(self):
        if not self.trace.exists():
            return []
        result = []
        for line in self.trace.read_text().splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # only an in-progress final write, not a skipped scenario
        return result

    def state(self):
        return next((e for e in reversed(self.events()) if e["type"] == "event"), None)

    def catalog(self):
        return wait(
            lambda: next((e for e in self.events() if e["type"] == "catalog"), None),
            "catalog trace",
        )

    def session(self):
        return Path(self.catalog()["session"])

    def ini(self, name):
        try:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string((self.session() / name).read_text())
            return parser
        except (OSError, configparser.Error):
            return None

    def paths(self):
        return [Path(s["path"]) for s in self.catalog()["sources"]]

    def cursor(self, expected):
        wait(lambda: self.state() and self.state()["cursor"] == expected, f"cursor {expected}")

    def keys_cursor(self, expected, *keys):
        self.s.key(self.wid, *keys)
        self.cursor(expected)

    def goto(self, index):
        self.s.key(self.wid, "Home", *("l" for _ in range(index)))
        self.cursor(index)

    def marks(self, expected):
        wait(lambda: set(self.state()["marked"]) == expected, f"marks {expected}")

    def dialog(self):
        return wait(lambda: self.s.windows(title="Move to Trash?"), "real GTK confirmation")[0]

    def poster(self, path):
        digest = hashlib.md5(("file://" + str(path)).encode(), usedforsecurity=False).hexdigest()
        return (
            Path(self.s.env["XDG_CACHE_HOME"])
            / "media-browser/thumbnails/posters"
            / f"{digest}.png"
        )

    def posters(self):
        return [self.poster(p) for p in self.paths() if self.poster(p).exists()]

    def quit(self):
        session = self.session()
        self.s.key(self.wid, "q")
        assert self.p.wait(timeout=20) == 0
        wait(lambda: self.wid not in self.s.windows(), "gallery exit")
        assert not session.exists(), "session endpoints must be removed"
        self.reaped()

    def members(self):
        members = []
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                if os.getpgid(int(path.name)) == self.p.pid:
                    members.append((int(path.name), (path / "comm").read_text().strip()))
            except (OSError, ProcessLookupError):
                pass
        return members

    def reaped(self):
        wait(
            lambda: not self.members(),
            "all app-owned renderers/viewers/decoders reaped",
            timeout=10,
        )


def quick_labels(count):
    """Independent quick-label oracle, never used by the application."""
    alphabet = "asdfghjkl"
    base, length = len(alphabet), 1
    while base**length < count:
        length += 1
    power = base ** (length - 1)
    high = base - count // power
    if high * (power // base) + (base - high) * power < count:
        high -= 1
    sequence = [1] * length + [0]
    if length > 1 and high > 0:
        sequence[length - 1] = 0
    labels = {}
    for index in range(count):
        labels["".join(alphabet[digit - 1] for digit in sequence if digit)] = index
        position = length
        while sequence[position] == 0:
            position -= 1
        sequence[position] += 1
        while sequence[position] > base and position > 0:
            sequence[position] = 1
            position -= 1
            sequence[position] += 1
        if position == 0 and sequence[0] - 1 == high:
            sequence[1:length] = [1] * (length - 1)
    return labels


def main():
    def deadline(signum, frame):
        raise TimeoutError("GUI scenario exceeded its readiness deadline")

    signal.signal(signal.SIGALRM, deadline)
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default="/usr/bin/media-browser")
    parser.add_argument("--geometry", default="1024x768")
    parser.add_argument("--compositor", choices=["none", "picom"], default="none")
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--scenario", action="append")
    args = parser.parse_args()
    names = [
        "discovery",
        "help_theme",
        "help_search",
        "gallery_search",
        "clipboard_paste",
        "navigation_selection",
        "fit_lifecycle",
        "first_fit_fast_format",
        "confirmation_trash",
        "partial_failure",
        "image_formats_orientation",
        "legacy_cache",
        "video_playback",
        "gif_playback",
        "cache_progressive",
        "thumbnail_publication_race",
        "cache_mutations",
        "stale_generation",
        "cancellation",
    ]
    if args.scenario and not set(args.scenario) <= set(names):
        parser.error("unknown scenario")
    if os.environ.get("MEDIA_BROWSER_REQUIRE_FULL") and (
        args.scenario or args.binary != "/usr/bin/media-browser"
    ):
        parser.error("CI requires every scenario against the installed command")
    suite = None
    try:
        suite = Suite.__new__(Suite)
        suite.__init__(args)
        for name in names:
            if args.scenario is None or name in args.scenario:
                suite.scenario(name, getattr(suite, name))
        suite.finish()
        print(f"PASS {len(suite.ran)} scenarios", flush=True)
    except Exception:
        traceback.print_exc()
        if suite and getattr(suite, "x", None):
            try:
                signal.alarm(10)
                suite.shot(suite.x.screen().root.id, "failure-screen")
            except Exception:
                traceback.print_exc()
            finally:
                signal.alarm(0)
        raise
    finally:
        if suite:
            suite.close()


if __name__ == "__main__":
    main()
