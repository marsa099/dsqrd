#!/usr/bin/env bash
# Run dsqrd with our own Nix Python env (no endcord, no slk).
cd "$(dirname "$0")" || exit 1
exec nix-shell --quiet shell.nix --run "python3 dsqrd.py"
