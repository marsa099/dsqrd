#!/usr/bin/env bash
set -e

target=$1
type=${2:-img}
mapfile -t files <<< "$target"

case "$type" in
    img|gif)
        exec setsid -f imv "${files[@]}"
        ;;
    video|mix)
        exec setsid -f mpv --loop --no-terminal "${files[@]}"
        ;;
    audio)
        exec setsid -f mpv --no-terminal --force-window=immediate --keep-open=yes --loop-file=no "${files[@]}"
        ;;
    *)
        exec xdg-open "$target"
        ;;
esac
