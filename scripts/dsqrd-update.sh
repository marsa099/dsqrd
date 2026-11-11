#!/usr/bin/env bash
set -euo pipefail
prefix=@PREFIX@
state="${XDG_STATE_HOME:-$HOME/.local/state}/dsqrd"
mkdir -p "$state"
# Save output even when invoked from the UI without a terminal. The manager
# owns the update lock and refuses dirty checkouts or unresolved merges.
if "$prefix/bin/dsqrd-manage" update --prefix "$prefix" >>"$state/update.log" 2>&1; then
    command -v notify-send >/dev/null && notify-send dsqrd 'Update installed' || true
else
    status=$?
    if [ "$status" -eq 20 ]; then
        command -v notify-send >/dev/null && notify-send dsqrd 'Merge conflict — repair opened in Herdr arch/dsqrd-update' || true
        exit 0
    fi
    command -v notify-send >/dev/null && notify-send -u critical dsqrd "Update failed — see $state/update.log" || true
    exit "$status"
fi
