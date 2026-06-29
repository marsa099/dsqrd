#!/usr/bin/env python3
"""dqs — read-only spike of a Discord backend for the native QML client.

Reuses endcord's Gateway + REST + token store, and speaks the SAME newline-
delimited-JSON Unix-socket protocol as slqs (the Go Slack daemon), so the
existing QML UI can render Discord with no client changes. This spike is
READ-ONLY: it serves workspaces (guilds) / channels / recent / live messages,
but does not send, edit, react, or mark anything.

    python3 dqs.py          # connects via your stored Discord token
    socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/dqs.sock   # eyeball the stream
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dchat import client_properties, discord as discord_mod, gateway as gateway_mod, token
from dchat.notifier import Notifier

SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "dsqrd.sock")
GIT_REV = os.environ.get("DSQRD_REV", "")   # baked build rev; empty on source runs


def _data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "dsqrd")
    os.makedirs(d, exist_ok=True)
    return d


def _seed_codemap():
    """codemap.json is shipped read-only beside dsqrd.py (the daemon never
    generates it — slqs does). Copy it into the writable XDG data dir on first
    run so dsqrd.py and the QML both read the same per-app path."""
    dst = os.path.join(_data_dir(), "codemap.json")
    if not os.path.exists(dst):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codemap.json")
        try:
            shutil.copyfile(src, dst)
        except OSError as e:
            print(f"dsqrd: codemap seed failed ({e!r})", flush=True)
    return dst


def _load_codemap():
    """Standard emoji shortcode -> unicode glyph, shared with the GUI's picker.
    codemap.json is keyed by colon-wrapped name (":thumbsup:"); the react command
    sends the bare name ("thumbsup"), so the colons are stripped here. Discord's
    reaction API needs the actual glyph, not the shortcode."""
    path = _seed_codemap()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k.strip(":"): v for k, v in raw.items()}
    except Exception as e:
        print(f"dsqrd: codemap load failed ({e!r}) — standard-emoji reactions disabled", flush=True)
        return {}
TEXT_CHANNEL_TYPES = {0, 5}  # 0=text, 5=announcements (skip voice/category/thread/forum for the spike)
DM_WS = "@me"                # synthetic "workspace" that holds all direct messages


def _token_from(store):
    """endcord stores either a bare [profile,...] list or {selected, profiles:[...]}."""
    if isinstance(store, dict):
        profiles = store.get("profiles") or []
        sel = store.get("selected")
        for p in profiles:
            if p.get("name") == sel and p.get("token"):
                return p["token"]
    else:
        profiles = store or []
    for p in profiles:
        if p.get("token"):
            return p["token"]
    return None


def load_token():
    """Token from the keyring (secret-tool) else plaintext profiles.json."""
    try:
        raw = token.load_secret()
        tok = _token_from(json.loads(raw)) if raw else None
        if tok:
            return tok
    except Exception:
        pass
    tok = _token_from(token.load_plain("~/.config/dsqrd/profiles.json"))
    if tok:
        return tok
    sys.exit("dsqrd: no Discord token found (put one in ~/.config/dsqrd/profiles.json)")


CDN = "https://cdn.discordapp.com"


def avatar_url(user_id, avatar_hash):
    if not user_id or not avatar_hash:
        return ""
    return f"{CDN}/avatars/{user_id}/{avatar_hash}.png?size=64"


def icon_url(guild_id, icon_hash):
    if not guild_id or not icon_hash:
        return ""
    return f"{CDN}/icons/{guild_id}/{icon_hash}.png?size=64"


def emoji_url(emoji_id, animated=False):
    if not emoji_id:
        return ""
    return f"{CDN}/emojis/{emoji_id}.{'gif' if animated else 'png'}?size=48"


EMOJI_JSON = os.path.join(_data_dir(), "emoji-dsqrd.json")


IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".apng")
UNFURL_TYPES = ("rich", "article", "link")


def _clean(u):
    return (u or "").split("?")[0].lower()


def _looks_image(u):
    return _clean(u).endswith(IMG_EXT)


def _derive_gif(url):
    """Tenor/Giphy expose the animated gif to Discord only as mp4 (no QtMultimedia
    here). Both host a real .gif at a derivable URL: Tenor swaps the 5-char size code
    to AAAAM (full gif); Giphy swaps .mp4 -> .gif on the same path."""
    if not url:
        return None
    m = re.match(r"(https?://(?:media[0-9]*|c)\.tenor\.com/)([A-Za-z0-9]+)/([^/?]+)\.(?:mp4|png|webp|gif)", url)
    if m and len(m.group(2)) >= 5:
        return f"{m.group(1)}{m.group(2)[:-5]}AAAAM/{m.group(3)}.gif"
    m = re.match(r"(https?://media[0-9]*\.giphy\.com/.+/giphy)\.(?:mp4|webp)", url)
    if m:
        return m.group(1) + ".gif"
    return None


def map_embeds(m, content):
    """Pull inline images/gifs and textual unfurls out of a normalized message.

    prepare_message folds uploaded attachments AND link previews into `embeds`:
    attachments carry a mimetype `type` (image/png, image/gif, …); real embeds
    carry type rich/article/link/image/gifv/video plus main_url/proxy_url.
    """
    imgs, unfurls = [], []
    mid = str(m.get("id", ""))
    for e in m.get("embeds", []) or []:
        t = str(e.get("type") or "")
        url = e.get("url") or ""
        main = e.get("main_url") or ""
        proxy = e.get("proxy_url") or ""
        hw = e.get("hw") or (0, 0)
        # uploaded attachment (mimetype type), or a bare CDN image link
        if t.startswith("image") or (t == "unknown" and _looks_image(url)):
            # A posted CDN link folds in with no `url` (only a signed proxy/thumbnail);
            # the bare CDN link 404s now that Discord signs attachment URLs. Fall back
            # to the signed proxy/main so the image actually loads.
            src = url or proxy or main
            gif = t == "image/gif" or _clean(src).endswith((".gif", ".apng"))
            imgs.append({"path": src, "full": src, "w": hw[1] or 0, "h": hw[0] or 0,
                         "id": mid, "ext": "", "type": "gif" if gif else "img", "pending": False})
            continue
        # uploaded video file (mimetype type video/*): placeholder card + play
        # badge; `v` runs do_view -> downloads the CDN url -> media-viewer.sh -> mpv.
        if t.startswith("video/"):
            vurl = url or proxy or main
            if vurl:
                cu = _clean(vurl).lower()
                ext = "mp4"
                for cand in ("mp4", "webm", "mov", "mkv"):
                    if cu.endswith("." + cand):
                        ext = cand
                        break
                # Discord's media proxy renders a still frame as JPEG (?format=jpeg);
                # use it as the poster. UI falls back to a plain ▶ card if it 404s.
                thumb = ""
                src = proxy or url
                if "discordapp" in src:
                    base = src.replace("cdn.discordapp.com", "media.discordapp.net")
                    thumb = base + ("&" if "?" in base else "?") + "format=jpeg"
                imgs.append({"path": thumb, "full": vurl, "w": hw[1] or 0, "h": hw[0] or 0,
                             "id": mid, "ext": ext, "type": "video", "pending": False})
            continue
        # link/media embed whose main image is a real image (unfurl image)
        if main and _looks_image(main):
            gif = _clean(main).endswith((".gif", ".apng"))
            imgs.append({"path": proxy or main, "full": main, "w": hw[1] or 0, "h": hw[0] or 0,
                         "id": mid, "ext": "", "type": "gif" if gif else "img", "pending": False})
            continue
        # gifv / video embed (Tenor, Giphy): Discord serves these as video, but
        # dchat exposes a proxied preview (often the animated gif). Show it via
        # AnimatedImage — animates if it's a gif, static otherwise. (No mp4
        # playback: this Quickshell build has no QtMultimedia.)
        if t in ("gifv", "video"):
            media = proxy or main
            # Derive the hosted .gif from the mp4 so it animates via AnimatedImage
            # (Discord gives no playable gif and there's no mp4 playback here).
            gif_url = _derive_gif(main or url)
            if gif_url:
                imgs.append({"path": gif_url, "full": gif_url, "w": hw[1] or 0, "h": hw[0] or 0,
                             "id": mid, "ext": "", "type": "gif", "pending": False})
            elif media and _looks_image(media):
                # Static thumbnail (YouTube .jpg) → Image; routing stills through
                # AnimatedImage made a later embed reuse an earlier one's frame.
                gif = _clean(media).endswith((".gif", ".apng"))
                imgs.append({"path": media, "full": media, "w": hw[1] or 0, "h": hw[0] or 0,
                             "id": mid, "ext": "", "type": "gif" if gif else "img", "pending": False})
            continue
        # link/article/rich unfurl (GitHub, x.com, news, …): Discord proxies a
        # preview image into proxy_url + hw while main_url is the article link.
        # Show that proxied image — it's a real image regardless of extension, so
        # don't gate it on _looks_image (GitHub's OG image has no extension).
        if t in UNFURL_TYPES:
            if proxy and (hw[0] or hw[1]):
                imgs.append({"path": proxy, "full": main or proxy, "w": hw[1] or 0, "h": hw[0] or 0,
                             "id": mid, "ext": "", "type": "img", "pending": False})
            # url = "<link>\n> title\n> description". Skip it when that prose is already
            # in the body — e.g. a GitHub/webhook post that spells out the issue AND
            # triggers Discord's auto-embed of the same link (that was the double-render).
            # Still add genuinely-new unfurl text for a bare posted link.
            u = url.strip()
            prose = " ".join(ln.lstrip("> ").strip() for ln in u.split("\n")[1:]).strip()
            if u and u != (content or "").strip() and not (prose and prose[:40] in (content or "")):
                unfurls.append(u)
    if imgs and "youtu" in (content or "").lower():
        print(f"dsqrd-dbg mid={mid} content={ (content or '')[:60]!r} "
              f"embeds={[(str(e.get('type')), (e.get('proxy_url') or e.get('main_url') or '')[:90]) for e in (m.get('embeds') or [])]} "
              f"imgs={[(i['type'], i['path'][:90]) for i in imgs]}", flush=True)
    return imgs, unfurls


def initials(name):
    parts = [p for p in re.split(r"[ ._-]+", name) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def hhmm(iso):
    # Discord timestamps are ISO8601 UTC; show them in local time.
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except Exception:
        return iso[11:16] if len(iso) >= 16 else ""


def daykey(iso):
    # YYYYMMDD in local time — matches the UI's date-divider key (Backend._dk),
    # so dsqrd messages group by real day instead of by garbage snowflake math.
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y%m%d")
    except Exception:
        return ""


MY_ID = ""   # own user id, set at startup; lets map_msg tag self-authored messages
USER_NAMES = {}   # id -> display name (mention fallback when not in m["mentions"])
CHAN_NAMES = {}   # id -> channel name (for <#id> mentions)


def _resolve_mentions(content, m):
    """Turn Discord <@id>/<#id>/<@&id> tokens into @name/#name wrapped in the
    client's mention markers ( normal,  self,  end) so the UI
    styles them like Slack mentions."""
    if not content:
        return content
    names = {str(u.get("id")): (u.get("global_name") or u.get("username") or "someone")
             for u in (m.get("mentions") or [])}

    def urepl(mt):
        uid = mt.group(1)
        nm = names.get(uid) or USER_NAMES.get(uid) or "someone"
        return ("" if uid == MY_ID else "") + "@" + nm + ""

    content = re.sub(r"<@!?(\d+)>", urepl, content)
    content = re.sub(r"<#(\d+)>", lambda mt: "#" + (CHAN_NAMES.get(mt.group(1)) or "channel") + "", content)
    content = re.sub(r"<@&\d+>", "@role", content)
    return content


def map_msg(m):
    """A normalized endcord message dict → the client's message shape."""
    author = m.get("nick") or m.get("global_name") or m.get("username") or "someone"
    uid = str(m.get("user_id") or "")
    if uid and author != "someone":
        USER_NAMES[uid] = author   # learn channel participants for the @-autocomplete
    content = _resolve_mentions(m.get("content", "") or "", m)
    # reply context (Discord replies carry the parent as referenced_message)
    reply_author = reply_text = reply_to_ts = ""
    ref = m.get("referenced_message")
    if isinstance(ref, dict):
        reply_to_ts = str(ref.get("id") or "")
        reply_author = ref.get("nick") or ref.get("global_name") or ref.get("username") or ""
        rt = (ref.get("content") or "").replace("\n", " ").strip()
        if not rt and ref.get("embeds"):
            rt = "[attachment]"
        if not rt and not reply_author:
            rt = "(deleted message)"
        reply_text = rt[:90]
    imgs, unfurls = map_embeds(m, content)
    body = content
    if unfurls:
        body = (body + "\n" + "\n".join(unfurls)).strip()
    rx = []
    for r in m.get("reactions", []) or []:
        eid = r.get("emoji_id")
        rx.append({"e": r.get("emoji", ""), "n": r.get("count", 0),
                   "name": r.get("emoji", ""), "mine": bool(r.get("me")),
                   "eid": eid or "",   # custom-emoji id, for the name:id reactor fetch
                   "img": emoji_url(eid, r.get("emoji_animated", False)) if eid else ""})
    link = ""
    mm = re.search(r"(https?://\S+)", content)
    if mm:
        link = mm.group(1)
    return {
        "author": author, "initials": initials(author), "color": "#7DD3FC",
        "avatar": avatar_url(m.get("user_id"), m.get("avatar")), "time": hhmm(m.get("timestamp", "")),
        "text": body, "grouped": False,
        "reactionsJson": json.dumps(rx), "imagesJson": json.dumps(imgs),
        "link": link, "ts": str(m.get("id", "")), "reply_count": 0,
        "replyAuthor": reply_author, "replyText": reply_text, "replyToTs": reply_to_ts,
        "day": daykey(m.get("timestamp", "")),
        "mine": MY_ID != "" and str(m.get("user_id", "")) == MY_ID,
    }


class DQS:
    def __init__(self):
        self.token = load_token()
        cp = client_properties.get_default_properties()
        self.user_agent = cp["browser_user_agent"]
        cp_gateway = client_properties.add_for_gateway(cp)
        cp_enc = client_properties.encode_properties(cp)
        self.discord = discord_mod.Discord(self.token, None, cp_enc, self.user_agent)
        self.gateway = gateway_mod.Gateway(self.token, None, cp_gateway, self.user_agent)
        self.guilds = []          # [{id,name,channels:[...]}]
        self.dms = []             # [{id,type,name,recipients}]
        self.chan_guild = {}      # channel id -> workspace id (guild_id or DM_WS)
        self.chan_name = {}       # channel id -> name
        self.dm_group_name = {}   # group-DM channel id -> display name (notifications)
        self.my_id = ""           # own user id (skip self-notify, detect mentions)
        self.active_ch = None     # channel the client is currently viewing (suppress its notifications)
        self.emoji_by_name = {}   # custom emoji name -> (id, animated) for react/send resolution
        self.codemap = _load_codemap()   # standard shortcode name -> unicode glyph (for reactions)
        self.notifier = None      # dbus notifier (clickable → open channel)
        self.app_active = False   # is our client window currently focused?
        self.user_names = {}      # user id -> display name (for DM typing indicators)
        self.pending_attach = {}  # channel id -> uploaded attachment, sent with next message
        self.uploading = {}       # channel id -> Event set when an in-flight upload finishes
        self.conns = []
        self.lock = threading.Lock()
        self.update_event = None   # latest updateAvailable event, replayed to new clients

    # ---- wire helpers ----
    def write(self, conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            self.drop(conn)

    def broadcast(self, obj):
        line = (json.dumps(obj) + "\n").encode()
        with self.lock:
            for c in list(self.conns):
                try:
                    c.sendall(line)
                except OSError:
                    self.conns.remove(c)

    def drop(self, conn):
        with self.lock:
            if conn in self.conns:
                self.conns.remove(conn)
        try:
            conn.close()
        except OSError:
            pass

    # ---- startup snapshot ----
    def wait_ready(self):
        for _ in range(120):
            if self.gateway.get_ready():
                break
            time.sleep(0.5)
        # get_guilds returns the list once, then None until it changes
        for _ in range(40):
            g = self.gateway.get_guilds()
            if g:
                self.guilds = g
                break
            time.sleep(0.25)
        for g in self.guilds:
            for ch in g.get("channels", []):
                self.chan_guild[ch["id"]] = g["guild_id"]
                self.chan_name[ch["id"]] = ch.get("name", "")
        try:
            self.my_id = self.gateway.get_my_id() or ""
        except Exception:
            self.my_id = ""
        global MY_ID, USER_NAMES, CHAN_NAMES
        MY_ID = self.my_id          # so map_msg can tag self-authored messages
        USER_NAMES = self.user_names  # same dict objects — later mutations reflect
        CHAN_NAMES = self.chan_name   # for <#id> mention resolution
        try:
            self.dms = self.gateway.get_dms()[0] or []
        except Exception:
            self.dms = []
        self._build_emoji()
        for dm in self.dms:
            self.chan_guild[dm["id"]] = DM_WS
            self.chan_name[dm["id"]] = dm.get("name", "")
            recips = dm.get("recipients", []) or []
            # Group DM (type 3 / >1 recipient): remember a name so notifications
            # show the group. Discord omits the auto-generated name for unnamed
            # groups, so fall back to joined recipient display names.
            if dm.get("type") == 3 or len(recips) > 1:
                self.dm_group_name[dm["id"]] = dm.get("name") or ", ".join(
                    (r.get("global_name") or r.get("username") or "")
                    for r in recips if r.get("id"))
            for r in recips:
                if r.get("id"):
                    self.user_names[str(r["id"])] = r.get("global_name") or r.get("username") or ""
        print(f"dsqrd: {len(self.guilds)} guilds, {len(self.dms)} DMs, {len(self.chan_name)} channels", flush=True)

    def _build_emoji(self):
        """Index guild custom emoji (name -> id/animated) and write the picker
        map (emoji-dsqrd.json: {guild_id: {name: cdn_url}}) the client watches."""
        ws_emoji = {}
        try:
            for g in self.gateway.get_emojis() or []:
                gid = g.get("guild_id")
                m = {}
                for e in g.get("emojis", []):
                    name, eid = e.get("name"), e.get("id")
                    if not name or not eid:
                        continue
                    anim = e.get("animated", False)
                    self.emoji_by_name[name] = (eid, anim)
                    m[name] = emoji_url(eid, anim)
                if m:
                    ws_emoji[gid] = m
            with open(EMOJI_JSON, "w") as f:
                json.dump(ws_emoji, f)
            print(f"dsqrd: {len(self.emoji_by_name)} custom emoji", flush=True)
        except Exception as e:
            print(f"dsqrd: emoji build error {e!r}", flush=True)

    def users_payload(self):
        # Flat known-user list (id -> name), offered under every workspace so the
        # client's @-autocomplete has candidates regardless of which guild/DM is open.
        lst = [{"name": n, "id": uid} for uid, n in self.user_names.items() if n]
        wss = [DM_WS] + [g["guild_id"] for g in self.guilds]
        return {ws: lst for ws in wss}

    def send_bootstrap(self, conn):
        if self.update_event:   # replay update-available state to a (re)connecting client
            self.write(conn, self.update_event)
        # Direct Messages is a synthetic workspace, listed first so it is the default.
        wss = [{"id": DM_WS, "name": "Direct Messages", "icon": ""}]
        wss += [{"id": g["guild_id"], "name": g.get("name", "?"),
                 "icon": icon_url(g["guild_id"], g.get("icon"))} for g in self.guilds]
        self.write(conn, {"type": "workspaces", "workspaces": wss, "rail": True, "threads": False})
        self.write(conn, {"type": "users", "users": self.users_payload()})
        entries = []
        for dm in self.dms:
            rec = (dm.get("recipients") or [{}])[0]
            entries.append({
                "id": dm["id"], "name": dm.get("name", ""), "kind": "dm",
                "topic": "", "unread": 0, "mention": False,
                "avatar": avatar_url(rec.get("id"), rec.get("avatar")), "workspace": DM_WS,
            })
        for g in self.guilds:
            gid = g["guild_id"]
            for ch in g.get("channels", []):
                if ch.get("type") not in TEXT_CHANNEL_TYPES:
                    continue
                entries.append({
                    "id": ch["id"], "name": ch.get("name", ""), "kind": "channel",
                    "topic": ch.get("topic") or "", "unread": 0, "mention": False, "avatar": "", "workspace": gid,
                })
        self.write(conn, {"type": "channels", "channels": entries, "subThreads": []})

    def guild_for(self, channel_id):
        """Guild id for a channel, or None for DMs (the @me synthetic workspace)."""
        ws = self.chan_guild.get(channel_id)
        return None if (ws is None or ws == DM_WS) else ws

    def send_recent(self, conn, channel_id):
        # Tell the gateway this is the active channel so it surfaces live events for it.
        try:
            self.gateway.subscribe(channel_id, self.guild_for(channel_id))
        except Exception:
            pass
        msgs = self.discord.get_messages(channel_id, num=50) or []
        out = [map_msg(m) for m in msgs]
        out.reverse()  # API returns newest-first; client wants chronological
        if not out:
            self.write(conn, {"type": "recent", "channel": channel_id, "msgs": [], "reset": True, "final": True})
            return
        chunk = 20
        for i in range(0, len(out), chunk):
            payload = {"type": "recent", "channel": channel_id, "msgs": out[i:i + chunk]}
            if i == 0:
                payload["reset"] = True
            if i + chunk >= len(out):
                payload["final"] = True
            self.write(conn, payload)
        # message authors just landed in user_names — refresh @-autocomplete candidates.
        self.write(conn, {"type": "users", "users": self.users_payload()})

    def send_history(self, conn, channel_id, before):
        msgs = self.discord.get_messages(channel_id, num=50, before=before) or []
        out = [map_msg(m) for m in msgs]
        out.reverse()
        self.write(conn, {"type": "history", "channel": channel_id, "msgs": out})

    def do_reactors(self, channel_id, ts, emojis):
        """Fetch who reacted (Discord's gateway omits the user list). One API call
        per emoji; custom emoji need the name:id form."""
        out = []
        for em in emojis:
            name = em.get("name", "")
            if not name:
                continue
            form = f"{name}:{em['eid']}" if em.get("eid") else name
            try:
                users = self.discord.get_reactions(channel_id, ts, form) or []
            except Exception as e:
                print(f"dsqrd: get_reactions error {e!r}", flush=True)
                users = []
            names = [(u.get("global_name") or u.get("username") or "someone") for u in users]
            out.append({"name": name, "users": names})
        self.broadcast({"type": "reactors", "channel": channel_id, "ts": str(ts), "reactions": out})

    def refresh_reactions(self, channel_id, message_id):
        """Reaction events carry only a delta; refetch the message for accurate state."""
        msgs = self.discord.get_messages(channel_id, num=1, around=message_id) or []
        for m in msgs:
            if str(m.get("id")) == str(message_id):
                self.broadcast({"type": "reaction", "channel": channel_id,
                                "ts": str(message_id), "reactionsJson": map_msg(m)["reactionsJson"]})
                return

    # ---- write commands ----
    def _call(self, label, fn, *fargs):
        """Run a write action in the background and log its result."""
        try:
            r = fn(*fargs)
            print(f"dsqrd: {label} -> {'ok' if r else 'FAILED'} ({r!r})", flush=True)
        except Exception as e:
            print(f"dsqrd: {label} EXC {e!r}", flush=True)

    def sub_emoji(self, text):
        """Picker/typed :name: → Discord <:name:id> for our known custom emoji."""
        def repl(mm):
            t = self.emoji_by_name.get(mm.group(1))
            if not t:
                return mm.group(0)
            eid, anim = t
            return f"<{'a' if anim else ''}:{mm.group(1)}:{eid}>"
        return re.sub(r":([A-Za-z0-9_]+):", repl, text or "")

    def do_send(self, channel_id, text, thread):
        # If an image is still uploading for this channel, wait for it so it
        # attaches to THIS message (not the next one).
        ev = self.uploading.get(channel_id)
        if ev and not ev.is_set():
            ev.wait(timeout=20)
        text = self.sub_emoji(text)
        att = self.pending_attach.pop(channel_id, None)   # staged image, if any
        atts = [att] if att else None
        if not text and not att:
            return   # nothing to send (defensive; UI guards empty + no attachment)
        if thread:
            self._call(f"send-reply ch={channel_id}", self.discord.send_message, channel_id, text,
                       thread, channel_id, self.guild_for(channel_id), True, atts)
        else:
            self._call(f"send ch={channel_id}", self.discord.send_message, channel_id, text,
                       None, None, None, True, atts)

    def do_view(self, conn, images, mediatype):
        """Download the message's full-res media to a dedicated, easy-to-purge dir
        (~/.cache/dsqrd/view) and tell the client to open them (viewReady). A photo
        set opens together so imv can page between them. Entries in one message
        share the message id, so the loop index disambiguates filenames."""
        viewdir = os.path.expanduser("~/.cache/dsqrd/view")
        os.makedirs(viewdir, exist_ok=True)
        paths = []
        for i, im in enumerate(images):
            url = im.get("url")
            if not url:
                continue
            ident = im.get("id") or "v"
            ext = im.get("ext") or os.path.splitext(url.split("?")[0])[1].lstrip(".") or "png"
            dest = os.path.join(viewdir, f"{ident}-{i}.{ext}")
            try:
                if not os.path.exists(dest):
                    req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                    with urllib.request.urlopen(req, timeout=20) as r, open(dest, "wb") as f:
                        f.write(r.read())
                paths.append(dest)
            except Exception as e:
                print(f"dsqrd: view error {e!r}", flush=True)
        if paths:
            self.write(conn, {"type": "viewReady", "paths": paths, "mediatype": mediatype})

    def set_presence(self, active):
        """Report desktop activity to Discord's gateway (op 3). afk=False while
        active holds mobile push (you see it on desktop); afk=True when idle lets
        Discord route notifications to your phone, so you don't miss them away."""
        try:
            if active:
                d = {"since": 0, "activities": [], "status": "online", "afk": False}
            else:
                d = {"since": int(time.time() * 1000), "activities": [], "status": "idle", "afk": True}
            self.gateway.send({"op": 3, "d": d})
            print(f"dsqrd: presence -> {'active' if active else 'idle (afk)'}", flush=True)
        except Exception as e:
            print(f"dsqrd: presence error {e!r}", flush=True)

    def do_upload_clipboard(self, channel_id, thread, ev):
        """Paste (Ctrl+V): grab via wl-paste + upload, then STAGE it (do not send).
        The image goes out with the next message. Reports progress via attachReady.
        `ev` (the in-flight-upload Event) is created and registered by the caller so
        a racing send waits for it; we just set() it when done (in finally)."""
        def fail():
            self.broadcast({"type": "attachReady", "channel": channel_id, "name": "", "ok": False})
        try:
            types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True).stdout
            mime = next((m for m in ("image/png", "image/jpeg", "image/gif", "image/webp") if m in types), None)
            if not mime:
                print("dsqrd: clipboard has no image", flush=True)
                return fail()
            tmp = f"/tmp/dsqrd-paste.{mime.split('/')[1]}"
            with open(tmp, "wb") as f:
                subprocess.run(["wl-paste", "--type", mime], stdout=f, check=True)
            # Show an "uploading" state immediately + hand the UI the local file.
            self.broadcast({"type": "attachUploading", "channel": channel_id,
                            "name": os.path.basename(tmp), "path": "file://" + tmp})
            att, code = self.discord.request_attachment_url(channel_id, tmp)
            if code != 0 or not att:
                print(f"dsqrd: attachment url failed (code {code})", flush=True)
                return fail()
            if not self.discord.upload_attachment(att["upload_url"], tmp):
                print("dsqrd: upload failed", flush=True)
                return fail()
            att["name"] = os.path.basename(tmp)
            self.pending_attach[channel_id] = att
            self.broadcast({"type": "attachReady", "channel": channel_id, "name": att["name"], "ok": True})
        except Exception as e:
            print(f"dsqrd: upload error {e!r}", flush=True)
            fail()
        finally:
            ev.set()   # release any send that's waiting on this upload

    # ---- loops ----
    def drain_gateway(self):
        while True:
            try:
                self._drain_one()
            except Exception as e:
                print(f"dsqrd: drain error {e!r}", flush=True)
                time.sleep(0.2)

    def _drain_one(self):
        ev = self.gateway.get_messages()
        if not ev:
            time.sleep(0.2)
            return
        op = ev.get("op")
        if op not in ("MESSAGE_CREATE", "MESSAGE_CREATE_QUICK", "MESSAGE_UPDATE",
                      "MESSAGE_DELETE", "MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"):
            return
        m = ev.get("d")
        if not isinstance(m, dict):
            return
        cid = m.get("channel_id")
        ws = self.chan_guild.get(cid)
        if ws is None:
            # Channel not registered at startup (e.g. a DM/channel opened later):
            # resolve its workspace from the event and register it, so its live
            # messages — including our own sent echo — broadcast instead of being
            # dropped (which forced a channel-switch + re-fetch to see them).
            ws = m.get("guild_id") or DM_WS
            self.chan_guild[cid] = ws
        if op in ("MESSAGE_CREATE", "MESSAGE_CREATE_QUICK", "MESSAGE_UPDATE"):
            self.broadcast({"type": "message", "workspace": ws, "channel": cid,
                            "thread": "", "mention": False, "msg": map_msg(m)})
            if op != "MESSAGE_UPDATE":
                self.maybe_notify(m, cid, ws)
        elif op == "MESSAGE_DELETE":
            self.broadcast({"type": "delete", "channel": cid, "ts": str(m.get("id"))})
        elif op in ("MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"):
            threading.Thread(target=self.refresh_reactions,
                             args=(cid, m.get("id")), daemon=True).start()

    def maybe_notify(self, m, cid, ws):
        """Fire a desktop notification for DMs and @mentions, unless it's the
        channel currently open or our own message. app-name 'endcord' so the
        quickshell bar's existing Discord counter picks it up."""
        if not isinstance(m, dict):
            return
        is_dm = ws == DM_WS
        mentioned = m.get("mention_everyone") or any(
            str(u.get("id")) == str(self.my_id) for u in (m.get("mentions") or []))
        if str(m.get("user_id")) == str(self.my_id):
            return
        # suppress only when the client window is focused AND viewing this channel
        if self.app_active and cid == self.active_ch:
            return
        if not (is_dm or mentioned):
            return
        author = m.get("nick") or m.get("global_name") or m.get("username") or "someone"
        if is_dm:
            grp = self.dm_group_name.get(cid, "")
            where = f" in {grp}" if grp else ""   # 1:1 DMs have no group name
        else:
            where = f" in #{self.chan_name.get(cid, '')}"
        body = (m.get("content") or "").replace("\n", " ").strip()[:140] or "(attachment)"
        title = f"{author}{where}"
        if self.notifier:
            self.notifier.notify(title, body, (ws, cid))   # clickable → opens the channel
        else:
            try:
                subprocess.Popen(["notify-send", "--app-name", "Discord", title, body],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"dsqrd: notify error {e!r}", flush=True)

    def _on_notif_activate(self, route):
        """A clicked notification routes here: open its channel in the client."""
        ws, cid = route
        self.broadcast({"type": "open", "workspace": ws, "channel": cid, "thread": ""})

    def drain_typing(self):
        while True:
            ev = self.gateway.get_typing()
            if not ev:
                time.sleep(0.3)
                continue
            cid = ev.get("channel_id")
            if cid not in self.chan_guild or str(ev.get("user_id")) == str(self.my_id):
                continue
            who = (ev.get("nick") or ev.get("global_name") or ev.get("username")
                   or self.user_names.get(str(ev.get("user_id")))
                   or self.chan_name.get(cid) or "someone")
            self.broadcast({"type": "typing", "channel": cid, "user": who})

    def watch_focus(self):
        """Track whether our client window is focused (suppress its notifications
        only then). Polls niri's focused window title."""
        while True:
            try:
                out = subprocess.run(["niri", "msg", "--json", "focused-window"],
                                     capture_output=True, text=True, timeout=3).stdout
                w = json.loads(out) if out.strip() else None
                active = bool(w) and (w.get("title") == "discord-client")
            except Exception:
                active = False
            self.app_active = active   # used to suppress notifications for the open channel
            time.sleep(1)

    def heartbeat(self):
        while True:
            time.sleep(3)
            self.broadcast({"type": "ping"})

    def read_conn(self, conn):
        buf = b""
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    cmd = json.loads(line)
                except ValueError:
                    continue
                t = cmd.get("type")
                ch = cmd.get("channel")
                if t in ("recent", "focus"):
                    self.active_ch = ch or None
                if t == "recent":
                    threading.Thread(target=self.send_recent, args=(conn, ch), daemon=True).start()
                elif t == "history":
                    threading.Thread(target=self.send_history, args=(conn, ch, cmd.get("before")), daemon=True).start()
                elif t == "send" and ch:
                    # allow attachment-only sends (empty text); do_send guards the
                    # genuinely-empty case (the UI never sends empty + no attachment).
                    threading.Thread(target=self.do_send, args=(ch, cmd.get("text", ""), cmd.get("thread")), daemon=True).start()
                elif t == "edit" and ch and cmd.get("ts"):
                    threading.Thread(target=self._call, args=("edit", self.discord.update_message, ch, cmd["ts"], cmd.get("text", "")), daemon=True).start()
                elif t == "delete" and ch and cmd.get("ts"):
                    threading.Thread(target=self._call, args=("delete", self.discord.delete_message, ch, cmd["ts"]), daemon=True).start()
                elif t == "react" and ch and cmd.get("ts") and cmd.get("emoji"):
                    emoji = cmd["emoji"]
                    if emoji in self.emoji_by_name:   # custom emoji → name:id
                        emoji = f"{emoji}:{self.emoji_by_name[emoji][0]}"
                    elif emoji in self.codemap:       # standard shortcode → unicode glyph
                        emoji = self.codemap[emoji]
                    fn = self.discord.remove_reaction if cmd.get("remove") else self.discord.send_reaction
                    threading.Thread(target=self._call, args=("react", fn, ch, cmd["ts"], emoji), daemon=True).start()
                elif t == "markread" and ch and cmd.get("before"):
                    threading.Thread(target=self._call, args=("markread", self.discord.ack, ch, cmd["before"]), daemon=True).start()
                elif t == "typing" and ch:
                    threading.Thread(target=self.discord.send_typing, args=(ch,), daemon=True).start()
                elif t == "view" and (cmd.get("images") or cmd.get("url")):
                    imgs = cmd.get("images") or [{"id": cmd.get("id"), "url": cmd.get("url"), "ext": cmd.get("ext", "")}]
                    threading.Thread(target=self.do_view, args=(conn, imgs, cmd.get("mediatype", "img")), daemon=True).start()
                elif t == "presence":
                    threading.Thread(target=self.set_presence, args=(cmd.get("state") != "idle",), daemon=True).start()
                elif t == "uploadClipboard" and ch:
                    # Register the upload synchronously so a "send" read next waits
                    # for the staged image instead of racing past it (text-only).
                    ev = threading.Event()
                    self.uploading[ch] = ev
                    threading.Thread(target=self.do_upload_clipboard, args=(ch, cmd.get("thread"), ev), daemon=True).start()
                elif t == "dropAttach" and ch:
                    self.pending_attach.pop(ch, None)
                elif t == "reactors" and ch and cmd.get("ts"):
                    threading.Thread(target=self.do_reactors,
                                     args=(ch, cmd["ts"], cmd.get("emojis") or []), daemon=True).start()
                # focus is a no-op (tracked above for notification suppression)
        self.drop(conn)

    def serve(self):
        if os.path.exists(SOCK):
            os.remove(SOCK)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK)
        srv.listen(8)
        print(f"dsqrd: streaming on {SOCK}", flush=True)
        while True:
            conn, _ = srv.accept()
            with self.lock:
                self.conns.append(conn)
            self.send_bootstrap(conn)
            threading.Thread(target=self.read_conn, args=(conn,), daemon=True).start()

    def check_updates(self):
        """Poll the repo's main SHA and tell the client when a newer build exists.
        Detect-only — applying is the host's job (flake bump + rebuild). Quiet on
        source runs (DSQRD_REV unset). Conditional ETag requests stay well under
        GitHub's unauthenticated 60/h limit."""
        if not GIT_REV:
            return
        api = "https://api.github.com/repos/daphen/dsqrd/commits/main"
        etag = None
        while True:
            try:
                headers = {"User-Agent": "dsqrd", "Accept": "application/vnd.github.sha"}
                if etag:
                    headers["If-None-Match"] = etag
                with urllib.request.urlopen(urllib.request.Request(api, headers=headers), timeout=15) as r:
                    etag = r.headers.get("ETag") or etag
                    latest = r.read().decode().strip()
                if latest and latest != GIT_REV:
                    self.update_event = {"type": "updateAvailable",
                                         "current": GIT_REV[:7], "latest": latest[:7]}
                    self.broadcast(self.update_event)
            except urllib.error.HTTPError as e:
                if e.code != 304:   # 304 = unchanged (ETag hit); anything else: retry next cycle
                    pass
            except Exception:
                pass
            time.sleep(3 * 3600)

    def run(self):
        threading.Thread(target=self.gateway.connect, daemon=True).start()
        self.wait_ready()
        try:
            self.notifier = Notifier("Discord", self._on_notif_activate)
        except Exception as e:
            print(f"dsqrd: notifier init failed ({e!r}); notifications disabled", flush=True)
            self.notifier = None
        threading.Thread(target=self.drain_gateway, daemon=True).start()
        threading.Thread(target=self.drain_typing, daemon=True).start()
        threading.Thread(target=self.watch_focus, daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        threading.Thread(target=self.check_updates, daemon=True).start()
        self.serve()


if __name__ == "__main__":
    DQS().run()
