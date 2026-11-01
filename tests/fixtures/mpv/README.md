# mpv fixtures (tests only)

These minimal synthetic fixtures exercise ordinary mpv configuration discovery
inside the harness's private HOME/XDG tree. `mpv.conf` loops generated clips for
playback observation. `input.conf` provides an example custom binding without
depending on a personal profile, scripts, fonts, history or logging preferences.
The harness appends null audio, software video and JSON IPC observation settings.

Assertions verify the exact original opens, decodes and plays in real mpv and
that closing it preserves the gallery. Keymap preference matching is not part
of application acceptance.

Nothing in this directory is installed by PKGBUILD. Production mpv receives the
original file argument and resolves its own ordinary configuration and scripts.
