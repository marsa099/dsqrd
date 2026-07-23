# Repository instructions

## Preserve fork features

This repository is Martin's fork of `daphen/dsqrd`. When merging upstream, preserve both upstream changes and fork-only functionality, especially the Microsoft Copilot catch-up UI (`ui/CopilotPanel.qml`) and its Composer, Backend, daemon, and shell wiring.

## Finish and deploy changes

For ordinary completed changes in this repository:

1. Run appropriate checks, commit the work, merge it into `main`, and push `main` to the `fork` remote (`marsa099/dsqrd`).
2. Deploy the exact pushed `main` revision by running `~/.scripts/update-dsqrd`. Do not manually guess or edit the Nix store path or pinned revision. The script updates the `dsqrd` input in `~/.config/nixos/flake.lock`, runs `nixos-rebuild switch`, and restarts the dsqrd daemon and UI.
3. Verify deployment completed and that the NixOS lock, installed package, and running daemon use the pushed `main` revision. If pushing, lock updating, rebuilding, or restarting fails, stop and report the failure; do not claim the task is complete.

The NixOS configuration consumes `github:marsa099/dsqrd`, not the local checkout, so pushing must happen before deployment.

### Update-script conflict handoff

`~/.scripts/update-dsqrd` may invoke pi while an upstream merge is already in progress and explicitly ask it only to resolve conflicts and commit. In that handoff, do **not** push, update the lock, rebuild, or restart from pi: stop after the merge commit so the waiting parent update script can perform those remaining steps. This exception prevents recursively invoking the updater and supersedes the ordinary deployment steps above.
