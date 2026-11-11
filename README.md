# slqs & dsqrd — native Slack & Discord clients for Wayland

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


Two keyboard-driven desktop chat clients that share one [Quickshell](https://quickshell.org) QML UI:

- **slqs** — Slack client. Go daemon.
- **dsqrd** — Discord client. Python daemon, built on endcord's `dchat` (gateway / REST / token store).

Each app is a small **daemon** that owns the network connection (websocket/gateway + REST), caches messages in SQLite, and pushes events to the **shared QML UI** over a Unix socket. The UI is a thin, vim-style renderer — `j/k` move, `i` insert/reply, `o` open link or mentioned channel, `v` view media, `r` react.

## What these are

Personal clients that log in with your **own session token** (Slack: `xoxc` token + `d` cookie; Discord: user token) and speak the services' **internal web/gateway APIs** — the same ones the official web clients use — rather than the public bot/app APIs. Those internal APIs are undocumented and change without notice, so expect occasional breakage.

Built for **Linux + Wayland** — the [niri](https://github.com/YaLTeR/niri) compositor and Quickshell — with paths hard-coded to the author's layout (`~/personal/...`, `~/.config/niri/scripts/...`). Not portable to macOS or Windows.

## Architecture

```
  Slack/Discord  ──ws+REST──►  daemon  ──unix socket (JSON lines)──►  Quickshell UI
                               (cache.db,                            (slk-gui-proto/*.qml)
                                notifications,
                                presence)
```

- **Daemon** (`slqs` binary / `dsqrd.py`): one persistent websocket/gateway connection, a SQLite cache, desktop notifications (dbus), and presence. Headless — no display needed.
- **UI**: a single Quickshell config launched twice — once per app, distinguished by `SLK_SOCK` (`slqs` vs `dsqrd`). It connects to the daemon's socket and renders. The UI is developed in `~/personal/slk-gui-proto/` and **vendored** into each repo's `ui/` via `sync-ui.sh`.
- **Launch / focus**: `~/.config/niri/scripts/launch-slack-client` / `launch-discord-client` ensure the daemon is running, reap stale UI instances, then `exec qs -p ~/personal/slk-gui-proto`.

## Dependencies

### Shared (runtime)
| Dependency | Used for |
|---|---|
| **Quickshell** (pulls in **Qt 6**) | the UI |
| **niri** (Wayland compositor) | launch/focus scripts call `niri msg`; window placement |
| **dbus + a notification server** (your Quickshell bar, or mako/dunst) | desktop notifications |
| **swayidle** | presence (active/away → gates phone notifications) |
| **wl-clipboard** (`wl-copy` / `wl-paste`) | paste/copy images |
| **imv** + **mpv** | viewing images/video (`v`), via `~/.config/endcord/media-viewer.sh` |

### slqs (Slack)
- **Go** (build-time). Produces a self-contained binary; all libraries (slack-go, gorilla/websocket, godbus, esiqveland/notify, …) are compiled in. Uses **pure-Go SQLite** (`modernc.org/sqlite`) — no system SQLite, no cgo.
- Runtime: the binary + the shared deps (dbus for notifications).

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

### dsqrd (Discord)
- **Nix** — `run-dsqrd.sh` launches it via `nix-shell shell.nix`.
- **Python 3** + (from `shell.nix`): `pysocks`, `websocket-client`, `filetype`, `protobuf`, `jeepney`.
- **notify-send** (notification fallback) and, optionally, **secret-tool** (keyring for the token).

## Build & install

**1. Install the shared deps** (adjust for your distro): `quickshell`, `niri`, `swayidle`, `wl-clipboard`, `imv`, `mpv`, a notification daemon — plus **Go** (for slqs) and **Nix** (for dsqrd).

**2. slqs**
```sh
cd ~/personal/slqs
go build -o slqs .        # self-contained binary, no system libs
```

**3. dsqrd** — no build step:
```sh
cd ~/personal/dsqrd
./run-dsqrd.sh            # nix-shell pulls the Python env, then runs dsqrd.py
```

**4. UI** — both apps load `~/personal/slk-gui-proto/`. After editing it, run `./sync-ui.sh` to vendor it into `slqs/ui/` and `dsqrd/ui/`.

**5. Media viewer** — `v` expects `~/.config/endcord/media-viewer.sh` (routes images to `imv`, video to `mpv`).

## Authentication

You provide your own session token; storage locations:

- **slqs (Slack)** — per-workspace files at `~/.local/share/slqs/tokens/<teamID>.json`:
  ```json
  { "access_token": "xoxc-…", "cookie": "<value of the `d` cookie>" }
  ```
  Both come from a logged-in Slack **web** session.

- **dsqrd (Discord)** — read from the system keyring (via `secret-tool`) or plaintext `~/.config/dsqrd/profiles.json` (endcord-compatible):
  ```json
  { "selected": "me", "profiles": [ { "name": "me", "token": "…" } ] }
  ```

These are credentials — keep the files private.

## Running

- **Open a client**: run the niri launch script (or bind it). It starts the daemon if needed and opens the UI.
- **Presence** (so your phone stays quiet while you're at the desk): swayidle drives `~/.config/niri/scripts/set-presence` — e.g. in `config.kdl`:
  ```kdl
  spawn-at-startup "swayidle" "-w" \
    "timeout" "300" "~/.config/niri/scripts/set-presence idle" \
    "resume" "~/.config/niri/scripts/set-presence active"
  ```
  slqs holds Slack "active" with a websocket `tickle` (the deprecated `users.setActive` is a no-op); dsqrd toggles the gateway `afk` flag. On idle they report away so mobile push resumes.

## Notes

- Internal APIs are undocumented and change without notice; some breakage is expected.
- Hard-coded paths assume `~/personal/{slqs,dsqrd,slk-gui-proto}` and `~/.config/niri/scripts`.
- Desktop notifications need a running dbus notification server; the daemon's dbus connection self-heals (reconnects) on failure.
- Slack mobile-push suppression depends on Slack's `push_idle_wait` being non-zero (it's the "send to mobile after N minutes idle" setting).
