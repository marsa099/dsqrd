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
import array
import atexit
import base64
import faulthandler
import hashlib
import html as _html
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

# The daemon died silently once (log just stopped) — make every exit path
# leave a trace: segfaults dump all stacks, normal exits log that they happened.
faulthandler.enable()
atexit.register(lambda: print("dsqrd: process exiting", flush=True))
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dchat import client_properties, discord as discord_mod, gateway as gateway_mod, token
from dchat.notifier import Notifier

SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "dsqrd.sock")
GIT_REV = os.environ.get("DSQRD_REV", "")   # baked build rev; empty on source runs


def _have_ffmpeg():
    """ffmpeg + ffprobe are hard runtime deps for gifs and voice notes: the
    daemon transcodes provider webm/mp4 to gif (this Qt decodes neither), builds
    voice waveforms, and records. The flake bundles them onto PATH; a bare
    source run must supply them. Cached; re-probes only while still missing so a
    late PATH fix is picked up without a restart."""
    if getattr(_have_ffmpeg, "_ok", False):
        return True
    ok = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
    _have_ffmpeg._ok = ok
    return ok


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


AVATAR_CACHE = os.path.expanduser("~/.cache/dsqrd/avatars")


def cached_avatar(user_id, avatar_hash):
    """Local path of a sender's avatar for notifications (the freedesktop
    image-path hint wants a file, not a URL); downloads and caches on miss.
    The hash is in the filename, so a changed avatar is fetched anew."""
    url = avatar_url(user_id, avatar_hash)
    if not url:
        return ""
    path = os.path.join(AVATAR_CACHE, f"{user_id}-{avatar_hash}.png")
    if os.path.exists(path):
        return path
    try:
        os.makedirs(AVATAR_CACHE, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f)
        return path
    except Exception:
        try:
            os.unlink(path)   # don't leave a truncated download behind
        except OSError:
            pass
        return ""


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


def _qt_img(url):
    """Inline display URL safe for this Qt build, which has no webp decoder.
    Discord's media proxy transcodes on demand — ask it for png when the
    inline path would be webp. Only for display paths; `full` keeps the
    original (imv/mpv decode webp fine)."""
    if url and ".webp" in _clean(url).lower() and re.search(r"(?:images-ext-\d+|media)\.discordapp\.net/", url):
        return url + ("&" if "?" in url else "?") + "format=png"
    return url


SPOTIFY_RE = re.compile(r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?(?:track|album|playlist|artist|episode|show)/[A-Za-z0-9]+[^\s]*")
_OEMBED_CACHE = {}


def _spotify_meta(url):
    """Song, artist, and album art for a Spotify link from its page's OpenGraph
    tags (no auth). Discord attaches embeds for album links but NOT track links
    (it renders those client-side), so we fetch the metadata ourselves. The
    og:description is "Artist · Album · Song · Year" — its first field is the
    artist. Cached (incl. failures)."""
    key = url.split("?")[0]
    if key in _OEMBED_CACHE:
        return _OEMBED_CACHE[key]
    res = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            page = r.read(80000).decode("utf-8", "ignore")   # og tags live in <head>

        def og(prop):
            m = re.search(r'<meta property="' + prop + r'" content="([^"]*)"', page)
            return _html.unescape(m.group(1)) if m else ""

        title, art, desc = og("og:title"), og("og:image"), og("og:description")
        artist = desc.split("·")[0].strip() if "·" in desc else ""
        if title:
            res = {"title": title, "artist": artist, "art": art}
    except Exception:
        res = None
    _OEMBED_CACHE[key] = res
    return res


def _fetch_text(url, cap=64 * 1024, show=4000):
    """Body of a small text attachment, truncated for display; "" on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read(cap + 1)
        if len(raw) > cap:
            return ""
        txt = raw.decode("utf-8", "replace").strip()
        if len(txt) > show:
            txt = txt[:show].rstrip() + "\n…"
        return txt
    except Exception:
        return ""


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
            disp = src
            if t == "image/webp" or _clean(src).lower().endswith(".webp"):
                # signed attachment CDN doesn't transcode; its media.* twin does
                disp = src.replace("cdn.discordapp.com", "media.discordapp.net")
                disp = _qt_img(disp)
            imgs.append({"path": disp, "full": src, "w": hw[1] or 0, "h": hw[0] or 0,
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
        # music unfurl (Spotify …): structured card — art + title + artist
        if t == "music":
            imgs.append({"type": "music", "art": proxy or main, "title": e.get("title", ""),
                         "artist": e.get("artist", ""), "provider": e.get("provider", "Spotify"),
                         "path": "", "full": main or "", "w": 0, "h": 0, "id": mid, "pending": False})
            continue
        # uploaded audio: voice messages arrive as audio/ogg attachments with
        # duration+waveform (flags bit 8192 on the raw message); plain audio
        # file uploads land here too. Render a playable pill; `v` downloads
        # and plays via media-viewer.sh -> mpv.
        if t.startswith("audio/"):
            aurl = url or proxy or main
            if aurl:
                cu = _clean(aurl).lower()
                ext = "ogg"
                for cand in ("ogg", "opus", "mp3", "m4a", "wav", "flac"):
                    if cu.endswith("." + cand):
                        ext = cand
                        break
                imgs.append({"path": "", "full": aurl, "w": 0, "h": 0, "id": mid,
                             "ext": ext, "type": "audio", "pending": False,
                             "name": e.get("name") or "",
                             "duration": int(round(e.get("duration_secs") or 0)),
                             "waveform": e.get("waveform") or ""})
            continue
        # link/media embed whose main image is a real image (unfurl image)
        if main and _looks_image(main):
            gif = _clean(main).endswith((".gif", ".apng"))
            imgs.append({"path": _qt_img(proxy or main), "full": main, "w": hw[1] or 0, "h": hw[0] or 0,
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
            vurl = e.get("video_url") or ""
            if gif_url:
                imgs.append({"path": gif_url, "full": gif_url, "w": hw[1] or 0, "h": hw[0] or 0,
                             "id": mid, "ext": "", "type": "gif", "pending": False})
            elif vurl and media:
                # No derivable public .gif (KLIPY sits behind Cloudflare) but
                # Discord proxies the mp4 — render the video card: transcoded
                # poster inline, `v` plays the proxied stream in mpv.
                imgs.append({"path": _qt_img(media), "full": vurl, "w": hw[1] or 0, "h": hw[0] or 0,
                             "id": mid, "ext": "mp4", "type": "video", "pending": False,
                             "gifv": True})   # upgradeable: queue_gifv converts to an inline gif
            elif media and _looks_image(media):
                # Static thumbnail (YouTube .jpg) → Image; routing stills through
                # AnimatedImage made a later embed reuse an earlier one's frame.
                gif = _clean(media).endswith((".gif", ".apng"))
                imgs.append({"path": _qt_img(media), "full": media, "w": hw[1] or 0, "h": hw[0] or 0,
                             "id": mid, "ext": "", "type": "gif" if gif else "img", "pending": False})
            continue
        # link/article/rich unfurl (GitHub, x.com, news, …): Discord proxies a
        # preview image into proxy_url + hw while main_url is the article link.
        # Show that proxied image — it's a real image regardless of extension, so
        # don't gate it on _looks_image (GitHub's OG image has no extension).
        # uploaded file that matched no media branch (message.txt overflow,
        # pdf, zip, …) — without this the message renders completely blank.
        # `name` is only set on real attachments, never on link embeds.
        name = e.get("name")
        if name and url:
            # Discord converts >2000-char messages into a message.txt
            # attachment: the file IS the message, so inline it. Signed CDN
            # urls expire (~24h) — a failed fetch falls back to the chip.
            if t.startswith("text/"):
                txt = _fetch_text(url)
                if txt:
                    unfurls.append(f" {name}\n{txt}")
                    continue
            unfurls.append(f" {name}\n{url}")
            continue
        if t in UNFURL_TYPES:
            if proxy and (hw[0] or hw[1]):
                imgs.append({"path": _qt_img(proxy), "full": main or proxy, "w": hw[1] or 0, "h": hw[0] or 0,
                             "id": mid, "ext": "", "type": "img", "pending": False})
            # url = "<link>\n> title\n> description". Drop the leading link line when the
            # body already has that link (the user posted the bare link; the embed just
            # restates it — that was the double link). Then drop the rest too if its prose
            # is already in the body (a webhook post that also auto-embeds). Append only
            # what's genuinely new.
            lines = url.split("\n") if url else []
            if lines and lines[0].strip() and lines[0].strip() in (content or ""):
                lines = lines[1:]
            u = "\n".join(lines).strip()
            prose = " ".join(ln.lstrip("> ").strip() for ln in lines).strip()
            if u and u != (content or "").strip() and not (prose and prose[:40] in (content or "")):
                unfurls.append(u)
    return imgs, unfurls


def _voice_meta(path):
    """Duration + Discord waveform (base64 u8 RMS buckets) for a voice note.
    Stdlib+ffmpeg only — dchat's helper needs numpy/soundfile, not in our env."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True).stdout.strip()
    duration = float(out or 0)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
        capture_output=True).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if not samples or not duration:
        return "", duration
    n = min(max(int(duration * 10), 32), 256)
    chunk = max(1, len(samples) // n)
    rms = []
    for i in range(n):
        seg = samples[i * chunk:(i + 1) * chunk]
        if not seg:
            break
        rms.append(math.sqrt(sum(s * s for s in seg) / len(seg)))
    peak = max(rms) or 1.0
    wf = bytes(min(255, int(v / peak * 255)) for v in rms)
    return base64.b64encode(wf).decode(), duration


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
        # Resolve <@id> tokens like the main content, but strip the mention
        # markers — the reply preview renders as plain Text, not rich.
        rt = _resolve_mentions(ref.get("content") or "", ref).replace("\n", " ").strip()
        rt = re.sub("[\\ue000\\ue001\\ue002]", "", rt)
        if not rt and ref.get("embeds"):
            rt = "[attachment]"
        if not rt and not reply_author:
            rt = "(deleted message)"
        reply_text = rt[:90]
    imgs, unfurls = map_embeds(m, content)
    # Spotify links Discord left un-embedded (track links especially — it renders
    # those client-side) get a card synthesized from Spotify's oEmbed metadata.
    if "open.spotify.com" in content:
        carded = {i.get("full", "").split("?")[0] for i in imgs if i.get("type") == "music"}
        for mo in SPOTIFY_RE.finditer(content):
            u = mo.group(0)
            if u.split("?")[0] in carded:
                continue
            meta = _spotify_meta(u)
            if meta:
                imgs.append({"type": "music", "art": meta.get("art", ""), "title": meta.get("title", ""),
                             "artist": meta.get("artist", ""), "provider": "Spotify", "path": "", "full": u,
                             "w": 0, "h": 0, "id": str(m.get("id", "")) + "-sp" + str(len(imgs)), "pending": False})
                carded.add(u.split("?")[0])
    body = content
    # a lone link that unfurled into an inline card/media (Spotify, gif, image)
    # doesn't also need its raw URL shown as text — render just the card. `link`
    # is still derived from the original content below, so `o` opens it.
    if imgs and re.sub(r"https?://\S+", "", content).strip() == "":
        body = ""
    if unfurls:
        body = (body + ("\n" if body else "") + "\n".join(unfurls)).strip()
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
        "author": author, "uid": str(m.get("user_id", "") or ""),
        "initials": initials(author), "color": "#7DD3FC",
        "avatar": avatar_url(m.get("user_id"), m.get("avatar")), "time": hhmm(m.get("timestamp", "")),
        "text": body, "grouped": False,
        "reactionsJson": json.dumps(rx), "imagesJson": json.dumps(imgs),
        "link": link, "ts": str(m.get("id", "")), "reply_count": 0,
        "replyAuthor": reply_author, "replyText": reply_text, "replyToTs": reply_to_ts,
        "day": daykey(m.get("timestamp", "")),
        "mine": MY_ID != "" and str(m.get("user_id", "")) == MY_ID,
        "edited": bool(m.get("edited")),
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
        self._presence_snap = {}  # uid -> "active"|"away" (friends/DMs)
        self._status_snap = {}    # uid -> status emoji (glyph or CDN URL)
        self.dm_group_name = {}   # group-DM channel id -> display name (notifications)
        self.my_id = ""           # own user id (skip self-notify, detect mentions)
        self.active_ch = None     # channel the client is currently viewing (suppress its notifications)
        self.chan_users = {}      # channel_id -> {uid: display name} — participants seen per channel
        self.emoji_by_name = {}   # custom emoji name -> (id, animated) for react/send resolution
        self.codemap = _load_codemap()   # standard shortcode name -> unicode glyph (for reactions)
        self.notifier = None      # dbus notifier (clickable → open channel)
        self.app_active = False   # is our client window currently focused?
        self.user_names = {}      # user id -> display name (for DM typing indicators)
        self.pending_attach = {}  # channel id -> uploaded attachment, sent with next message
        self.uploading = {}       # channel id -> Event set when an in-flight upload finishes
        self.voice_proc = None    # ffmpeg recording process while a voice note is being taken
        self.voice_channel = None
        self.voice_path = "/tmp/dsqrd-voice.ogg"
        self.voice_lock = threading.Lock()   # start/stop race → orphaned ffmpeg recorders
        self.play_proc = None     # ffplay process while a voice note plays in-line
        self.play_id = None       # message id being played (UI accents its pill)
        self.play_lock = threading.Lock()
        self.gif_gen = 0          # newest gif-browser request; stale conversions bail
        self._gifv_busy = set()   # inline-gif conversions in flight (dest paths)
        atexit.register(lambda: self.voice_proc and self.voice_proc.kill())
        atexit.register(lambda: self.play_proc and self.play_proc.kill())
        self.conns = []
        self.lock = threading.Lock()
        self.update_event = None   # latest updateAvailable event, replayed to new clients
        self._update_etag = None   # GitHub ETag: conditional requests are free
        self._last_update_check = 0.0

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

    def learn_participant(self, cid, m):
        """Record a message author as a participant of its channel. Returns
        True when the user is new to the channel (callers re-push users)."""
        uid = str(m.get("user_id") or "")
        name = m.get("nick") or m.get("global_name") or m.get("username")
        if not uid or not name:
            return False
        chan = self.chan_users.setdefault(cid, {})
        fresh = uid not in chan
        chan[uid] = name
        return fresh

    def users_payload(self, channel_id=None):
        # @-autocomplete candidates. Scoped to a channel's PARTICIPANTS when a
        # channel is given (Discord won't enumerate text-channel members for
        # user tokens, so people who've messaged here is the honest set);
        # the unscoped bootstrap list only bridges until a channel opens.
        if channel_id is not None:
            part = self.chan_users.get(channel_id, {})
            lst = [{"name": n, "id": uid} for uid, n in part.items() if n]
        else:
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
                "user": str(rec.get("id") or ""),
            })
        for g in self.guilds:
            gid = g["guild_id"]
            for ch in g.get("channels", []):
                if ch.get("type") not in TEXT_CHANNEL_TYPES:
                    continue
                entries.append({
                    "id": ch["id"], "name": ch.get("name", ""), "kind": "channel",
                    "topic": ch.get("topic") or "", "unread": 0, "mention": False, "avatar": "", "workspace": gid,
                    "user": "",
                })
        self.write(conn, {"type": "channels", "channels": entries, "subThreads": []})
        for ws in [DM_WS] + [g["guild_id"] for g in self.guilds]:
            if self._presence_snap:
                self.write(conn, {"type": "presence", "workspace": ws, "all": self._presence_snap})
            if self._status_snap:
                self.write(conn, {"type": "status", "workspace": ws, "all": self._status_snap})

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
        for m in msgs:
            self.learn_participant(channel_id, m)
        out = [map_msg(m) for m in msgs]
        for mm in out:
            self.queue_gifv(channel_id, mm)
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
        # Channel participants just landed — scope @-autocomplete to them.
        self.write(conn, {"type": "users", "users": self.users_payload(channel_id)})

    def send_history(self, conn, channel_id, before):
        msgs = self.discord.get_messages(channel_id, num=50, before=before) or []
        out = [map_msg(m) for m in msgs]
        for mm in out:
            self.queue_gifv(channel_id, mm)
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
        """Run a write action in the background and log its result. Failures
        also toast — a 413 (attachment over Discord's size cap) used to
        vanish silently and the message just never appeared."""
        act = label.split(" ")[0]
        try:
            r = fn(*fargs)
            print(f"dsqrd: {label} -> {'ok' if r else 'FAILED'} ({r!r})", flush=True)
            if not r:
                self.broadcast({"type": "toast", "text": f"{act} failed — Discord rejected it (attachment too large?)"})
        except Exception as e:
            print(f"dsqrd: {label} EXC {e!r}", flush=True)
            self.broadcast({"type": "toast", "text": f"{act} failed: {e}"})

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
        if att is not None:
            at = att.pop("_thread", "")   # the attachment carries its own thread
            if at:
                thread = at
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

    def voice_start(self, conn, channel_id):
        """Record a voice note off the default input (ffmpeg → ogg/opus)."""
        with self.voice_lock:
            if not channel_id or self.voice_proc:
                return
            try:
                os.remove(self.voice_path)
            except OSError:
                pass
            try:
                # -t caps a forgotten recording; SIGINT on stop finalizes the ogg
                proc = subprocess.Popen(
                    ["ffmpeg", "-y", "-v", "error", "-f", "pulse", "-i", "default",
                     "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "48k",
                     "-t", "600", self.voice_path],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.write(conn, {"type": "toast", "text": "voice: ffmpeg not available"})
                return
            self.voice_proc, self.voice_channel = proc, channel_id
        self.broadcast({"type": "voice", "state": "recording", "channel": channel_id})
        time.sleep(0.25)   # ffmpeg exits instantly when there's no input source
        if proc.poll() is not None and self.voice_proc is proc:
            self.voice_proc = None
            self.broadcast({"type": "voice", "state": "idle"})
            self.write(conn, {"type": "toast", "text": "voice: recording failed (no input source?)"})

    def voice_stop(self, conn, send):
        """Stop the running recording; send it as a Discord voice message or discard."""
        with self.voice_lock:
            proc = self.voice_proc
            if not proc:
                return
            self.voice_proc = None
            ch = self.voice_channel
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        if not send:
            self.broadcast({"type": "voice", "state": "idle"})
            try:
                os.remove(self.voice_path)
            except OSError:
                pass
            return
        self.broadcast({"type": "voice", "state": "sending"})
        try:
            if not (os.path.exists(self.voice_path) and os.path.getsize(self.voice_path) > 0):
                self.write(conn, {"type": "toast", "text": "voice: nothing recorded"})
                return
            waveform, duration = _voice_meta(self.voice_path)
            if duration < 1:
                self.write(conn, {"type": "toast", "text": "voice: too short — not sent"})
                return
            if not self.discord.send_voice_message(ch, self.voice_path, waveform=waveform, duration=duration):
                self.write(conn, {"type": "toast", "text": "voice: send failed"})
        finally:
            self.broadcast({"type": "voice", "state": "idle"})
            try:
                os.remove(self.voice_path)
            except OSError:
                pass

    def queue_gifv(self, channel_id, mm):
        """Upgrade gifv video-cards (KLIPY & co.) to inline animated gifs: the
        card shows immediately, the proxied stream converts to a local gif off
        this thread, then the message's images are live-replaced (the same
        `images` mechanism slqs uses for unfurl updates)."""
        if not _have_ffmpeg():
            return   # leaves the (still-useful) video card; do_gifs warns loudly
        try:
            imgs = json.loads(mm.get("imagesJson") or "[]")
        except Exception:
            return
        if any(i.get("gifv") for i in imgs):
            threading.Thread(target=self._gifv_convert, args=(channel_id, mm["ts"], imgs), daemon=True).start()

    def _gifv_convert(self, channel_id, ts, imgs):
        d = os.path.expanduser("~/.cache/dsqrd/gifpicker")
        os.makedirs(d, exist_ok=True)
        changed = False
        for i in imgs:
            if not i.get("gifv"):
                continue
            key = hashlib.md5(i["full"].encode()).hexdigest()[:16] + "-inline"
            dest = os.path.join(d, key + ".gif")
            if not os.path.exists(dest):
                if dest in self._gifv_busy:
                    # another message shares this gif — wait for that conversion
                    for _ in range(60):
                        if os.path.exists(dest):
                            break
                        time.sleep(0.25)
                    if not os.path.exists(dest):
                        continue
                else:
                    self._gifv_busy.add(dest)
                    src = os.path.join(d, key + ".src")
                    try:
                        req = urllib.request.Request(i["full"], headers={"User-Agent": self.user_agent})
                        with urllib.request.urlopen(req, timeout=20) as r, open(src, "wb") as f:
                            f.write(r.read())
                        p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                                            "-vf", "fps=15,scale=-2:280", "-loop", "0", dest],
                                           capture_output=True)
                        if p.returncode != 0 or not os.path.exists(dest):
                            continue
                    except Exception as e:
                        print(f"dsqrd: gifv convert error {e!r}", flush=True)
                        continue
                    finally:
                        self._gifv_busy.discard(dest)
                        try:
                            os.remove(src)
                        except OSError:
                            pass
            u = "file://" + dest
            i.update({"type": "gif", "path": u, "full": u, "ext": ""})
            i.pop("gifv", None)
            changed = True
        if changed:
            self.broadcast({"type": "images", "channel": channel_id, "ts": ts,
                            "imagesJson": json.dumps(imgs)})

    def do_gifs(self, conn, q, gen):
        """GIF browser (/gif in the composer): Discord's /gifs API (KLIPY-backed).
        The result list goes out immediately; previews convert webm -> small gif
        (this Qt can't decode klipy's animated webp) and stream in progressively."""
        from concurrent.futures import ThreadPoolExecutor
        if not _have_ffmpeg():
            self.write(conn, {"type": "gifs", "gen": gen, "items": []})
            self.write(conn, {"type": "toast", "text": "GIFs need ffmpeg — it's missing from the daemon's PATH"})
            print("dsqrd: ffmpeg/ffprobe not found — GIF browser and inline gifs disabled", flush=True)
            return
        self.gif_gen = gen
        d = os.path.expanduser("~/.cache/dsqrd/gifpicker")
        os.makedirs(d, exist_ok=True)
        try:   # keep the preview cache bounded
            fs = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime)
            for f in fs[:-300]:
                os.remove(f)
        except OSError:
            pass
        if q:
            items = self.discord.search_gifs(q)[:24]
            # gif result: selecting it sends the page url
            out = [{"id": g["id"] or g["url"], "title": g["title"], "url": g["url"],
                    "w": g["width"], "h": g["height"], "media": g["webm"]} for g in items]
        else:
            # empty query = trending category tiles; selecting one searches its name
            items = self.discord.trending_gifs()[:24]
            out = [{"id": "cat:" + c["name"], "title": c["name"], "url": "",
                    "category": True, "w": 0, "h": 0, "media": c["src"]} for c in items]
        self.write(conn, {"type": "gifs", "gen": gen, "items": out})
        with ThreadPoolExecutor(max_workers=3) as ex:
            for g in out:
                ex.submit(self._gif_preview, conn, gen, d, g["id"], g["media"])

    def _gif_preview(self, conn, gen, cachedir, item_id, media_url):
        # ffmpeg reads gif (category tiles) and webm (search results) alike and
        # normalizes both to a small looping gif Qt can animate.
        if self.gif_gen != gen or not media_url:
            return
        key = hashlib.md5(media_url.encode()).hexdigest()[:16]
        dest = os.path.join(cachedir, key + ".gif")
        if not os.path.exists(dest):
            src = os.path.join(cachedir, key + ".src")
            try:
                req = urllib.request.Request(media_url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=15) as r, open(src, "wb") as f:
                    f.write(r.read())
                p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                                    "-vf", "fps=12,scale=-2:180", "-loop", "0", dest],
                                   capture_output=True)
                if p.returncode != 0 or not os.path.exists(dest):
                    return
            except Exception as e:
                print(f"dsqrd: gif preview error {e!r}", flush=True)
                return
            finally:
                try:
                    os.remove(src)
                except OSError:
                    pass
        if self.gif_gen == gen:
            self.write(conn, {"type": "gifPreview", "gen": gen,
                              "id": item_id, "path": "file://" + dest})

    def play_audio(self, conn, msg_id, url, ext):
        """Play a voice note / audio attachment in-line (no window): download to
        the view cache, play daemon-side via ffplay. The UI accents the pill off
        the playback events; v again or q stops."""
        viewdir = os.path.expanduser("~/.cache/dsqrd/view")
        os.makedirs(viewdir, exist_ok=True)
        dest = os.path.join(viewdir, f"{msg_id}-a.{ext or 'ogg'}")
        try:
            if not os.path.exists(dest):
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=20) as r, open(dest, "wb") as f:
                    f.write(r.read())
        except Exception as e:
            print(f"dsqrd: play fetch error {e!r}", flush=True)
            self.write(conn, {"type": "toast", "text": "couldn't fetch audio"})
            return
        with self.play_lock:
            if self.play_proc and self.play_proc.poll() is None:
                self.play_proc.terminate()   # its watcher broadcasts the old idle
            try:
                proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", dest],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.write(conn, {"type": "toast", "text": "ffplay not available"})
                return
            self.play_proc, self.play_id = proc, msg_id
        self.broadcast({"type": "playback", "state": "playing", "id": msg_id})
        threading.Thread(target=self._watch_play, args=(proc, msg_id), daemon=True).start()

    def _watch_play(self, proc, msg_id):
        proc.wait()
        with self.play_lock:
            if self.play_proc is not proc:
                return   # superseded by a newer play
            self.play_proc = self.play_id = None
        self.broadcast({"type": "playback", "state": "idle", "id": msg_id})

    def play_stop(self):
        with self.play_lock:
            if self.play_proc and self.play_proc.poll() is None:
                self.play_proc.terminate()   # watcher does cleanup + idle broadcast

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
        def fail(reason=""):
            self.broadcast({"type": "attachReady", "channel": channel_id, "name": "", "ok": False, "err": reason})
        try:
            types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True).stdout
            mime = next((m for m in ("image/png", "image/jpeg", "image/gif", "image/webp") if m in types), None)
            if not mime:
                print("dsqrd: clipboard has no image", flush=True)
                return fail()
            tmp = f"/tmp/dsqrd-paste.{mime.split('/')[1]}"
            # The clipboard can advertise image/png a beat before the bytes are servable
            # (the source app exiting, clipse mid-store), so a single grab yields 0 bytes
            # and the upload 400s. Retry briefly to ride out that race.
            data = b""
            for _ in range(5):
                data = subprocess.run(["wl-paste", "--type", mime], capture_output=True).stdout
                if data:
                    break
                time.sleep(0.2)
            if not data:
                print("dsqrd: clipboard image grab was empty", flush=True)
                return fail("clipboard image was empty")
            with open(tmp, "wb") as f:
                f.write(data)
            # Show an "uploading" state + hand the UI the local file for the optimistic preview.
            self.broadcast({"type": "attachUploading", "channel": channel_id,
                            "name": os.path.basename(tmp), "path": "file://" + tmp})
            att, code = self.discord.request_attachment_url(channel_id, tmp)
            if code != 0 or not att:
                print(f"dsqrd: attachment url failed (code {code})", flush=True)
                return fail("Discord refused the upload (too large?)")
            if not self.discord.upload_attachment(att["upload_url"], tmp):
                print("dsqrd: upload failed", flush=True)
                return fail("upload failed")
            att["name"] = os.path.basename(tmp)
            att["_thread"] = thread or ""   # route to the thread it was staged in
            self.pending_attach[channel_id] = att
            self.broadcast({"type": "attachReady", "channel": channel_id, "name": att["name"], "ok": True})
        except Exception as e:
            print(f"dsqrd: upload error {e!r}", flush=True)
            fail(f"upload failed: {e}")
        finally:
            ev.set()   # release any send that's waiting on this upload

    def _compress_for_discord(self, path):
        """Shrink an image/video under Discord's ~10 MB cap. Returns the new
        path (in /tmp) or None if the type isn't compressible or it fails."""
        ext = os.path.splitext(path)[1].lower()
        out = os.path.join("/tmp", "dsqrd-compress-" + os.path.basename(path))
        try:
            if ext in (".mp4", ".mov", ".mkv", ".webm", ".m4v"):
                out = os.path.splitext(out)[0] + ".mp4"
                dur = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", path],
                    capture_output=True, text=True).stdout.strip()
                dur = float(dur or 0) or 1.0
                # aim ~8.5 MB total, 96k audio, 6% mux headroom
                vbr = int((8.5 * 8 * 1024 * 1024 / dur * 0.94) - 96000) // 1000
                vbr = max(200, vbr)
                r = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", path,
                     "-c:v", "libx264", "-b:v", f"{vbr}k", "-maxrate", f"{vbr}k",
                     "-bufsize", f"{vbr*2}k", "-preset", "medium", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "96k", out])
                if r.returncode != 0 or not os.path.exists(out):
                    return None
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                out = os.path.splitext(out)[0] + ".jpg"
                # cap the long edge and re-encode as jpeg; enough for screenshots
                r = subprocess.run(
                    ["magick", path, "-resize", "2560x2560>", "-quality", "82", out])
                if r.returncode != 0 or not os.path.exists(out):
                    return None
            else:
                return None
            if os.path.getsize(out) / (1024 * 1024) > 10:
                return None   # still too big — let the caller fall back to the toast
            return out
        except Exception as e:
            print(f"dsqrd: compress error {e!r}", flush=True)
            return None

    def do_compress_upload(self, channel_id, thread, path, ev):
        """Compress an oversized image/video, then hand off to the normal
        staging upload. Runs on its own thread; ev released in the finally."""
        try:
            self.broadcast({"type": "toast", "text": "Compressing…"})
            small = self._compress_for_discord(path)
            if not small:
                self.broadcast({"type": "attachReady", "channel": channel_id,
                                "name": "", "ok": False, "err": "couldn't compress under 10 MB"})
                return
            self._stage_upload(channel_id, thread, small, os.path.basename(path))
        finally:
            ev.set()

    COMPRESSIBLE = (".png", ".jpg", ".jpeg", ".webp", ".bmp",
                    ".mp4", ".mov", ".mkv", ".webm", ".m4v")

    def do_upload_file(self, channel_id, thread, path, ev):
        """Upload a file from disk (any type). Same staging flow as the paste —
        the file goes out with the next message."""
        def fail(reason=""):
            self.broadcast({"type": "attachReady", "channel": channel_id, "name": "", "ok": False, "err": reason})
        try:
            if not path or not os.path.isfile(path):
                print(f"dsqrd: uploadFile bad path {path!r}", flush=True)
                return fail("no file at that path")
            name = os.path.basename(path)
            mb = os.path.getsize(path) / (1024 * 1024)
            # Discord caps uploads at 10 MB without nitro and rejects the SEND
            # (413) after staging. If it's a compressible image/video, ask the
            # UI whether to shrink it instead of uploading a doomed file.
            if mb > 10:
                if os.path.splitext(name)[1].lower() in self.COMPRESSIBLE:
                    self.broadcast({"type": "askCompress", "channel": channel_id,
                                    "thread": thread or "", "path": path,
                                    "name": name, "mb": round(mb)})
                    return
                self.broadcast({"type": "attachReady", "channel": channel_id, "name": "",
                                "ok": False, "err": f"{name} is {mb:.0f} MB — over Discord's 10 MB limit"})
                return
            self._stage_upload(channel_id, thread, path, name)
        except Exception as e:
            print(f"dsqrd: uploadFile error {e!r}", flush=True)
            fail(f"upload failed: {e}")
        finally:
            ev.set()

    def _stage_upload(self, channel_id, thread, path, name):
        """Request an attachment URL, upload the bytes, stage for the next
        send. `name` is the display name (may differ from a temp path)."""
        def fail(reason=""):
            self.broadcast({"type": "attachReady", "channel": channel_id, "name": "", "ok": False, "err": reason})
        img = os.path.splitext(name)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        self.broadcast({"type": "attachUploading", "channel": channel_id,
                        "name": name, "path": ("file://" + path) if img else ""})
        att, code = self.discord.request_attachment_url(channel_id, path)
        if code != 0 or not att:
            print(f"dsqrd: attachment url failed (code {code})", flush=True)
            return fail("Discord refused the upload (too large?)")
        if not self.discord.upload_attachment(att["upload_url"], path):
            print("dsqrd: upload failed", flush=True)
            return fail("upload failed")
        att["name"] = name
        att["_thread"] = thread or ""   # route to the thread it was staged in
        self.pending_attach[channel_id] = att
        self.broadcast({"type": "attachReady", "channel": channel_id, "name": name, "ok": True})

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
            if self.learn_participant(cid, m) and cid == self.active_ch:
                self.broadcast({"type": "users", "users": self.users_payload(cid)})
            mm = map_msg(m)
            self.broadcast({"type": "message", "workspace": ws, "channel": cid,
                            "thread": "", "mention": False, "msg": mm})
            self.queue_gifv(cid, mm)
            if op != "MESSAGE_UPDATE":
                self.maybe_notify(m, cid, ws)
                self.maybe_mark_active_read(cid, m)
        elif op == "MESSAGE_DELETE":
            self.broadcast({"type": "delete", "channel": cid, "ts": str(m.get("id"))})
        elif op in ("MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"):
            threading.Thread(target=self.refresh_reactions,
                             args=(cid, m.get("id")), daemon=True).start()

    def maybe_mark_active_read(self, cid, m):
        """Ack a live message on the server when it lands in the channel you're
        actively viewing with the window focused — so reading on desktop clears
        the unread on your phone too. Without this the client only acked on
        channel-open, leaving messages read on desktop still badging mobile."""
        if str(m.get("user_id")) == str(self.my_id):
            return
        if not (self.app_active and cid == self.active_ch):
            return
        mid = m.get("id")
        if mid:
            threading.Thread(target=self.discord.ack, args=(cid, str(mid)), daemon=True).start()

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
            # Avatar download can block, so resolve it off the gateway thread;
            # clickable → opens the channel.
            uid, ah = m.get("user_id"), m.get("avatar")
            threading.Thread(
                target=lambda: self.notifier.notify(
                    title, body, (ws, cid), image=cached_avatar(uid, ah)),
                daemon=True).start()
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
                elif t == "gifs":
                    threading.Thread(target=self.do_gifs,
                                     args=(conn, str(cmd.get("q") or "").strip(), int(cmd.get("gen") or 0)), daemon=True).start()
                elif t == "play" and cmd.get("url"):
                    threading.Thread(target=self.play_audio,
                                     args=(conn, str(cmd.get("id") or "a"), cmd["url"], cmd.get("ext", "")), daemon=True).start()
                elif t == "playStop":
                    threading.Thread(target=self.play_stop, daemon=True).start()
                elif t == "voiceStart" and ch:
                    threading.Thread(target=self.voice_start, args=(conn, ch), daemon=True).start()
                elif t == "voiceStop":
                    threading.Thread(target=self.voice_stop, args=(conn, bool(cmd.get("send"))), daemon=True).start()
                elif t == "uploadClipboard" and ch:
                    # Register the upload synchronously so a "send" read next waits
                    # for the staged image instead of racing past it (text-only).
                    ev = threading.Event()
                    self.uploading[ch] = ev
                    threading.Thread(target=self.do_upload_clipboard, args=(ch, cmd.get("thread"), ev), daemon=True).start()
                elif t == "uploadFile" and ch:
                    ev = threading.Event()
                    self.uploading[ch] = ev
                    threading.Thread(target=self.do_upload_file, args=(ch, cmd.get("thread"), cmd.get("path"), ev), daemon=True).start()
                elif t == "compressUpload" and ch:
                    ev = threading.Event()
                    self.uploading[ch] = ev
                    threading.Thread(target=self.do_compress_upload, args=(ch, cmd.get("thread"), cmd.get("path"), ev), daemon=True).start()
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
        if not _have_ffmpeg():
            print("dsqrd: WARNING — ffmpeg/ffprobe not on PATH; GIFs and voice "
                  "messages will not work. Install the flake build or add ffmpeg.", flush=True)
        while True:
            conn, _ = srv.accept()
            with self.lock:
                self.conns.append(conn)
            # Bootstrap + serve on a per-connection thread so a slow or stuck client
            # can't block the accept loop — that previously left every client which
            # connected after it empty. (slqs already does its bootstrap off-thread.)
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn):
        try:
            conn.settimeout(15)     # a dead client must not hang the bootstrap forever
            self.send_bootstrap(conn)
            conn.settimeout(None)   # back to blocking for the command read loop
        except OSError:
            self.drop(conn)
            return
        # A fresh client (app (re)start) → re-check for updates unless we just
        # did. The warm daemon otherwise only re-checks every 3h, so an app
        # restart never surfaced a new build. Throttled + ETag-conditional.
        if GIT_REV and (time.time() - self._last_update_check) > 60:
            threading.Thread(target=self._check_update_once, daemon=True).start()
        self.read_conn(conn)

    def _check_update_once(self):
        """One update check against the repo's main SHA. ETag-conditional, so a
        304 (unchanged) is free against GitHub's 60/h unauthenticated limit."""
        if not GIT_REV:
            return
        self._last_update_check = time.time()
        api = "https://api.github.com/repos/daphen/dsqrd/commits/main"
        try:
            headers = {"User-Agent": "dsqrd", "Accept": "application/vnd.github.sha"}
            if self._update_etag:
                headers["If-None-Match"] = self._update_etag
            with urllib.request.urlopen(urllib.request.Request(api, headers=headers), timeout=15) as r:
                self._update_etag = r.headers.get("ETag") or self._update_etag
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

    def check_updates(self):
        """Tell the client when a newer build exists. Detect-only — applying is the
        host's job (flake bump + rebuild). Quiet on source runs (DSQRD_REV unset).
        Checks at start, then every 3h; also re-checked when a client connects
        (see _serve_conn) so restarting the app surfaces a new build immediately
        instead of waiting on the warm daemon's next poll."""
        if not GIT_REV:
            return
        while True:
            self._check_update_once()
            time.sleep(3 * 3600)

    def drain_presence(self):
        """Poll the gateway's friend/DM presence list (updated by
        READY_SUPPLEMENTAL + PRESENCE_UPDATE) into presence/status maps the
        UI understands, broadcast on change. Emitted per workspace id so a
        friend's status also shows on their guild-channel messages."""
        while True:
            acts = self.gateway.get_dm_activities()
            if not acts:
                time.sleep(1.0)
                continue
            presence, status = {}, {}
            for a in acts:
                uid = str(a.get("id") or "")
                if not uid:
                    continue
                presence[uid] = "active" if a.get("status") == "online" else "away"
                em = a.get("custom_status_emoji")
                if em:
                    if em.get("id"):
                        status[uid] = emoji_url(em["id"], em.get("animated", False))
                    elif em.get("name"):
                        status[uid] = em["name"]
            self._presence_snap, self._status_snap = presence, status
            self._broadcast_presence()
            time.sleep(1.0)

    def _broadcast_presence(self):
        wss = [DM_WS] + [g["guild_id"] for g in self.guilds]
        for ws in wss:
            if self._presence_snap:
                self.broadcast({"type": "presence", "workspace": ws, "all": self._presence_snap})
            if self._status_snap:
                self.broadcast({"type": "status", "workspace": ws, "all": self._status_snap})

    def watch_wake(self):
        """Detect suspend by wall-vs-monotonic clock divergence (monotonic
        pauses during suspend). Gateway events from the gap were never
        delivered — and after a long gap the session re-identifies, which
        replays nothing — so tell the UI to refetch what it's showing."""
        mono, wall = time.monotonic(), time.time()
        while True:
            time.sleep(5)
            m, w = time.monotonic(), time.time()
            if (w - wall) - (m - mono) > 60:
                print("dsqrd: wake from suspend — resync", flush=True)
                # kick the gateway immediately: a short gap can still RESUME
                # (Discord replays the missed events); a long one re-identifies
                # now instead of waiting out a heartbeat cycle. An expired
                # resume falls through to re-identify, and wait_online covers
                # a network that isn't back yet.
                self.gateway.resumable = True
                self.gateway.reconnect_requested = True
                time.sleep(5)   # let the network come back before clients refetch
                self.broadcast({"type": "resync"})
            mono, wall = m, w

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
        threading.Thread(target=self.drain_presence, daemon=True).start()
        threading.Thread(target=self.watch_focus, daemon=True).start()
        threading.Thread(target=self.watch_wake, daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        threading.Thread(target=self.check_updates, daemon=True).start()
        self.serve()


if __name__ == "__main__":
    DQS().run()
