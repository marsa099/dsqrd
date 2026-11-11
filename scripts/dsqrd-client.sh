#!/usr/bin/env bash
set -euo pipefail
prefix=@PREFIX@
app="$prefix/share/dsqrd"
state="${XDG_STATE_HOME:-$HOME/.local/state}/dsqrd"
mkdir -p "$state"
# Keep both daemon and UI startup serialized; neither inherits this lock.
exec 9>"$state/launch.lock"
flock -x 9
python="$prefix/share/dsqrd-venv/bin/python3"
alive=false
for proc in /proc/[0-9]*/cmdline; do
    mapfile -d '' -t args < "$proc" 2>/dev/null || continue
    if [[ ${args[0]:-} == "$python" && ${args[1]:-} == "$app/dsqrd.py" ]]; then
        alive=true
        break
    fi
done
if ! "$alive"; then
    rm -f "${XDG_RUNTIME_DIR:?}/dsqrd.sock"
    nohup "$prefix/bin/dsqrd" >>"$state/daemon.log" 2>&1 </dev/null 9>&- &
fi
export SLK_SOCK=dsqrd
export QML2_IMPORT_PATH="$app/ui/vendor${QML2_IMPORT_PATH:+:$QML2_IMPORT_PATH}"
export SLK_MEDIA_VIEWER="$app/media-viewer.sh"
# Preserve an explicit host override (including a NixOS update command).
export SLK_UPDATE_CMD="${SLK_UPDATE_CMD:-$(printf '%q' "$prefix/bin/dsqrd-update")}"
if reply=$(timeout 2s qs ipc -p "$app/ui" call window openWindow 2>/dev/null) && [ "$reply" = true ]; then
    exit 0
fi
# Wait for startup while holding the lock, so a second shortcut cannot race
# the new UI before it has registered its IPC target.
qs -p "$app/ui" 9>&- &
ui_pid=$!
for _ in {1..100}; do
    if reply=$(timeout 1s qs ipc -p "$app/ui" call window openWindow 2>/dev/null) && [ "$reply" = true ]; then
        break
    fi
    kill -0 "$ui_pid" 2>/dev/null || exit 1
    sleep 0.1
done
flock -u 9
exec 9>&-
wait "$ui_pid"
