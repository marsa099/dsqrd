"""Discord token loading — keyring (secret-tool) with a plaintext fallback.

Reads the token from dsqrd's own store (~/.config/dsqrd/profiles.json or the
'dsqrd' keyring service). A future own-login flow can replace this without
touching the rest of dchat.
"""
import json
import os
import subprocess

SERVICE = "dsqrd"   # keyring service the token lives under


def load_secret():
    """Token store JSON string from the keyring, or '' if unavailable."""
    result = subprocess.run(
        ["secret-tool", "lookup", "service", SERVICE],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def load_plain(profiles_path):
    """Token store list/dict from a plaintext profiles.json, or [] if absent."""
    path = os.path.expanduser(profiles_path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_secret(token_value):
    """Replace the selected keyring profile token, creating one when needed."""
    try:
        store = json.loads(load_secret() or "{}")
    except Exception:
        store = {}
    if isinstance(store, list):
        store = {"selected": "Discord", "profiles": store}
    profiles = store.setdefault("profiles", [])
    selected = store.get("selected") or "Discord"
    store["selected"] = selected
    profile = next((p for p in profiles if p.get("name") == selected), None)
    if profile is None:
        profile = {"name": selected}
        profiles.append(profile)
    profile["token"] = token_value
    subprocess.run(
        ["secret-tool", "store", "--label=dsqrd Discord profile", "service", SERVICE],
        input=json.dumps(store), text=True, check=True,
    )
