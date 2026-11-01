"""Read the real X11 clipboard in the harness's private display."""

import json
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
if sys.argv[1] == "text":
    print(json.dumps({"text": clipboard.wait_for_text()}))
else:
    image = clipboard.wait_for_image()
    if image is None:
        raise RuntimeError("No clipboard image")
    image.savev(sys.argv[2], "png", [], [])
    _, targets = clipboard.wait_for_targets()
    print(
        json.dumps(
            {
                "width": image.get_width(),
                "height": image.get_height(),
                "targets": [target.name() for target in targets],
            }
        )
    )
