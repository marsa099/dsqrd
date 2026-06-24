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
