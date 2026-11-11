# dsqrd — keyboard-driven Discord for Wayland

> **This is a fork of [daphen/dsqrd](https://github.com/daphen/dsqrd).**
> This fork adds a non-Nix install/update path with automatic upstream merges,
> fork-aware update detection, and a reopenable Quickshell window, alongside:
>
> | | What |
> |---|---|
> | `dsqrd-cli` | Stdlib-only Python CLI over the daemon socket — `channels`, `messages`, `summary`, `open`, `send`. Reading uses only the side-effect-free `history` command, never `recent`/`focus`, so it never clobbers the UI's active-channel or notification state. ([upstream issue #8](https://github.com/daphen/dsqrd/issues/8)) |
>
> `dsqrd-cli` is never packaged — it runs straight out of this checkout and
> talks to whatever daemon is up, and every command it uses exists upstream, so
> it works unchanged against a stock build.
>
> The launcher `pgrep` fix that used to live on this branch is upstream
> [PR #11](https://github.com/daphen/dsqrd/pull/11). Fork-specific update
> configuration remains relevant when installing this fork. The previous
> incarnation of this fork carried a Copilot catch-up panel and an issues
> panel; both were dropped once daphen shipped his own agent/summary feature.
> That history is preserved on the
> [`archive/copilot-fork`](../../tree/archive/copilot-fork) branch.


`dsqrd` is a native-feeling Discord desktop client built with a Python daemon
and a Quickshell/QML interface. It is designed for fast keyboard navigation,
compact message browsing, desktop notifications, media handling, and optional
conversation summaries.

> [!WARNING] dsqrd is an unofficial client. It uses Discord's private gateway
> and REST APIs, which may change without notice and may be subject to Discord's
> terms. Expect occasional breakage.

## Features

- Guild channels and direct messages
- Live gateway updates and recent-message history
- Send, reply, edit, delete, and react to messages
- Upload files, paste images, and record voice notes
- Open or save images, videos, GIFs, and attachments
- Threads, mentions, profiles, presence, mute controls, and voice channels
- Actionable desktop notifications
- Optional conversation summaries and follow-up questions
- Vim-style navigation with an in-app keybinding reference
- QR-code authentication through the official Discord mobile app

Press `?` in the app to open the current keybinding reference. The help view is
generated from the real keymap, so it remains authoritative as bindings change.

## Architecture

```text
Discord gateway + REST API
            │
            ▼
  Python daemon (dsqrd.py)
            │  newline-delimited JSON over $XDG_RUNTIME_DIR/dsqrd.sock
            ▼
     Quickshell/QML UI
```

The daemon owns authentication, gateway state, REST requests, notifications,
uploads, media conversion, and the Unix socket. The QML process owns rendering,
keyboard interaction, and local UI state. Keeping those responsibilities
separate allows the daemon to remain connected if the interface is restarted.

Compositor-specific launch, focus, and placement policy is intentionally outside
this repository. The packaged client starts one daemon and one UI instance but
does not depend on a particular Wayland compositor.

## Requirements

The Nix package provides the application runtime, including Python libraries,
Quickshell, FFmpeg, ImageMagick, Secret Service tooling, `mpv`, and `imv`. A
Linux Wayland session and a working Secret Service implementation are still
required.

### Arch / non-Nix installation and updates (this fork)

Install system prerequisites first (Arch):

```sh
sudo pacman -S --needed python python-pip git quickshell util-linux coreutils \
  ffmpeg imagemagick mpv imv wl-clipboard libnotify
# If quickshell is not in your enabled repositories, install it separately.
```

Use a checkout on `main` with `origin` pointing to your GitHub fork and
`upstream` pointing to `https://github.com/daphen/dsqrd.git`. Configure your Git
name/email so automatic merge commits can be created. Then:

```sh
make install-local
~/.local/bin/dsqrd-client
```

This installs a dedicated `~/.local/share/dsqrd-venv`, application files, and
`dsqrd`, `dsqrd-client`, `dsqrd-manage`, and `dsqrd-update` launchers in
`~/.local/bin`. It restarts the app and preserves runtime preferences/emoji
state. `PREFIX=/other/path make install-local` is also supported. The previous
application payload is retained in `PREFIX/share/.dsqrd-previous`.

The in-app **U** action invokes `dsqrd-update`, or run it manually:

```sh
~/.local/bin/dsqrd-update
# Detailed foreground output:
~/.local/bin/dsqrd-manage update
```

Updates fetch **both origin/main and upstream/main**, automatically merge both
into your local `main`, install, and restart dsqrd. There is **no automatic
push**: your GitHub fork is not modified. Merges are first attempted in a
throwaway worktree; conflicts leave your checkout and installed application
unchanged. Dirty/untracked work, a non-main checkout, fetch errors, and concurrent
updates stop the procedure. Commit/stash work first; resolve upstream conflicts
manually when necessary, or opt in to the terminal repair handoff below.
A later dependency/install failure can leave the local
checkout successfully merged while the previous application is still installed;
fix the error and rerun. Git hooks and upstream code execute with your user
permissions, just as with a manual source update.

#### Optional conflict-repair terminal

On a machine with Herdr and Pi installed, explicitly enable the repair handoff:

```sh
python3 scripts/local-install.py install --conflict-assistant herdr
```

The installer remembers the current Herdr socket, so updates invoked from the
Discord UI can reach the same session. On a genuine merge conflict, the updater
opens a new **dsqrd-update** tab in workspace **arch**, creating that workspace
in the repository directory if necessary. Pi starts with the explicit
`openai/gpt-6.1-sol` model and instructions to reproduce the pinned merges,
resolve conflicts while retaining fork/upstream functionality, check new Python
dependencies, run tests, commit the resolution, and retry installation. It never
instructs the repair session to push or discard user work. This delegates
repository write and app-restart permission to an interactive coding session;
its semantic merge decisions still require judgment and may need your input.

The running app remains untouched while repair is pending. Instructions live
outside the checkout at `~/.local/state/dsqrd/merge-instructions.md`. An existing
`arch/dsqrd-update` tab prevents duplicate repair agents; finish or close it
before requesting another. Startup/auth/approval errors are logged instead of
being reported as a successful handoff. No repair session opens on network
errors or dirty checkouts. Use `--no-assistant` on an individual update to fail
normally, or reinstall with `--conflict-assistant none` to disable the feature.
This optional integration does not change the NixOS update path.

The installer records the published fork/upstream commits actually contained in
the build, separately from its possibly unpublished local merge revision. Update
detection compares ancestry against those baselines: being locally ahead is not
an update, and an unchanged upstream tip does not keep the badge stuck on. Each
API request has its own ETag and cached response. Failed checks retain the last
known state rather than claiming "up to date". **Ctrl+Shift+R** checks immediately.
The changelog uses the published baseline for the advertised update leg.

Update output is saved in `${XDG_STATE_HOME:-~/.local/state}/dsqrd/update.log`;
notifications report success/failure when `notify-send` is installed. Override
`SLK_UPDATE_CMD` if you want a different host-specific workflow. NixOS continues
to use its host-provided rebuild command; this local updater does not modify Nix
configuration.

Tests (no network or running app required):

```sh
python3 -m unittest discover -s tests -v
# Optional live Niri test, closes/reopens the running Discord window:
python3 tests/test_window_reopen.py ~/.local/bin/dsqrd-client
```

### Python dependencies

The main Python dependencies (installed automatically by the local installer) are:

- `websocket-client`
- `pysocks`
- `filetype`
- `protobuf`
- `jeepney`
- `pycryptodome`
- `qrcode`
- `pillow`

## Run with Nix

Run the current checkout:

```sh
git clone https://github.com/daphen/dsqrd.git
cd dsqrd
nix run .
```

Build without launching:

```sh
nix build .#dsqrd
nix build .#dsqrd-client
```

The default package is `dsqrd-client`, which:

1. Starts the daemon if it is not already healthy.
1. Waits for `$XDG_RUNTIME_DIR/dsqrd.sock`.
1. Reuses an existing UI instead of launching a duplicate.
1. Starts the packaged Quickshell interface when needed.

For source-only daemon development, `./run-dsqrd.sh` enters the legacy
`shell.nix` environment and runs `dsqrd.py`. The flake remains the complete and
preferred runtime.

## Authentication

On first launch, dsqrd displays a QR code. Scan it with the official Discord
mobile app and approve the login there. The daemon stores the resulting session
in Secret Service; credentials are never exposed to QML.

An existing endcord-compatible `~/.config/dsqrd/profiles.json` is accepted as a
compatibility fallback, but QR authentication with Secret Service storage is the
supported path.

## Data and configuration

| Path                            | Purpose                                                  |
| ------------------------------- | -------------------------------------------------------- |
| `$XDG_RUNTIME_DIR/dsqrd.sock`   | Daemon-to-UI socket                                      |
| `~/.local/share/dsqrd/`         | Writable application data and preferences                |
| `~/.cache/dsqrd/`               | Avatars, previews, and temporary viewed media            |
| `~/.config/dsqrd/profiles.json` | Compatibility profile and optional summary configuration |
| `/tmp/dsqrd.log`                | Daemon output when started by the packaged launcher      |

Viewed media is cached under `~/.cache/dsqrd/view`; use the explicit save action
for files that should be kept.

## Optional integrations

### Conversation summaries

The catch-up menu can use an installed command-line provider or a configured API
provider. The first use opens an in-app setup flow and stores its choice in the
`summarize` block of `~/.config/dsqrd/profiles.json`.

### Custom message actions

Set `DSQRD_MESSAGE_ACTION` to expose an additional action for the selected
message. See [`docs/custom-message-actions.md`](docs/custom-message-actions.md)
for the versioned JSON payload contract.

## Development

Useful checks before submitting a change:

```sh
python -m py_compile dsqrd.py
nix build .#dsqrd
nix build .#dsqrd-client
```

The application code is organized as:

- `dsqrd.py` — daemon, socket protocol, Discord operations, and media handling
- `dchat/` — gateway, authentication, token storage, and protocol helpers
- `ui/` — the packaged Quickshell interface
- `flake.nix` — reproducible daemon and client packages
- `tests/` — focused authentication tests

## License and attribution

The Discord protocol layer began from endcord's `dchat` implementation and has
since been adapted for dsqrd's daemon and UI protocol. Check dependency sources
and file headers for their applicable licenses.
