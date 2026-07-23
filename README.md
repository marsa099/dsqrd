# slqs & dsqrd — native Slack & Discord clients for Wayland

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

## CLI (`dsqrd-cli`)

A stdlib-only Python script that talks to the running daemon's socket (same JSON-lines protocol as the UI, but only the side-effect-free `history` command — it never touches the daemon's active-channel/notification state):

```sh
dsqrd-cli channels [query]              # list channels
dsqrd-cli messages <name> [-n N]        # print the last N messages (default 50)
dsqrd-cli messages <name> --since-mine  # everything after my last message
dsqrd-cli summary <name>                # AI catch-up summary of --since-mine, via `pi -p` (pi's configured model, override with --model)
```

Channel names match case-insensitively (exact, then substring). `--since-mine` pages back up to 500 messages looking for a message authored by you.

## Copilot catch-up (dsqrd)

A **fork addition** — not in upstream `daphen/dsqrd`. A small button sits in the composer just left of send:

<!-- screenshot: the Copilot button in the composer (idle) -->

Tap it — or press `c` in a channel — and everything posted since you last read the channel is summarized into a single takeover "message" from Microsoft Copilot — main topics, who said what that matters, and anything directed at you to act on, kept as brief as possible. Copilot always receives the last 50 messages for conversational context, but the prompt explicitly treats messages before the captured read boundary as context only. The summary is generated in both English and Swedish in one shot (English first and shown by default); `l` flips between them (the current language and the keybind are shown in the card). `q` or `esc` dismisses it; if you're already caught up it just says so.

Press `f` on a summary to type feedback about it (`enter` sends, `esc` cancels). The feedback is handed to pi, which decides whether it warrants a standing rule and, if so, updates the curated rule list in `~/.local/share/dsqrd/copilot-feedback.md` — injected into every future summary prompt (the summary cache is cleared so the next `c` reflects the new rules).

Three things keep it fast without wasting prompts: the reply **streams** into the card as it's generated; summaries are **cached per channel** keyed on the newest message ts (reopening `c` with nothing new is instant and free); and a **speculative prefetch** fires — debounced, only when ≥5 unread messages arrived and the cache is stale — on channel switch and when the window regains niri focus (the daemon broadcasts `appActive` flips), so after being away the summary is usually already there when you press `c`.

<!-- screenshots: loading state · the summary takeover -->

Under the hood it sends the 50-message contextual transcript, including the last-read marker, to a tool-free, ephemeral `pi -p` invocation using pi's configured provider/model. The streaming UI consumes pi's JSON events, and `pi` must be on your `PATH`.

## Upstream issue tracker & link chooser (dsqrd)

Two more **fork additions**:

- **Issue tracker** — the daemon polls `daphen/dsqrd` (override with `DSQRD_ISSUE_REPO`) for issues filed by `DSQRD_ISSUE_AUTHOR` (default `marsa099`) and the statusbar shows a quiet `upstream: N open · M closed` line (a pending update takes the corner instead). Press `!` — or click the line — for a takeover listing the issues; `1-9` opens a row's GitHub page directly, `j/k` + `enter`/`l` and clicking work too, `q`/`esc` closes.
- **Link chooser** — `o` on a message containing several URLs pops a numbered chooser (same keys as above) instead of silently opening only the first link. One link keeps the instant-open.

## Notes

- This repo is a fork of [`daphen/dsqrd`](https://github.com/daphen/dsqrd) with local additions (see **Copilot catch-up**); upstream is merged in periodically.
- Internal APIs are undocumented and change without notice; some breakage is expected.
- Hard-coded paths assume `~/personal/{slqs,dsqrd,slk-gui-proto}` and `~/.config/niri/scripts`.
- Desktop notifications need a running dbus notification server; the daemon's dbus connection self-heals (reconnects) on failure.
- Slack mobile-push suppression depends on Slack's `push_idle_wait` being non-zero (it's the "send to mobile after N minutes idle" setting).
