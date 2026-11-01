#!/usr/bin/env bash
if [[ ${1:-} == --version ]]; then
    exec /usr/lib/media-browser/pqiv.real "$@"
fi
exec gdb -q -batch \
    -ex "set logging file $HOME/renderer-debug-$$.log" \
    -ex 'set logging enabled on' -ex run -ex 'thread apply all bt' \
    --args /usr/lib/media-browser/pqiv.real "$@"
