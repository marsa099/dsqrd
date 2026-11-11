import base64
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

import qrcode
import websocket
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

GATEWAY_URL = "wss://remote-auth-gateway.discord.gg/?v=2"
LOGIN_URL = "https://discord.com/api/v9/users/@me/remote-auth/login"


class RemoteAuth:
    def __init__(self, user_agent, emit):
        self.user_agent = user_agent
        self.emit = emit
        self.cancelled = threading.Event()
        self.ws = None
        self.private_key = RSA.generate(2048)
        self.qr_path = ""

    def cancel(self):
        self.cancelled.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _decrypt(self, value):
        cipher = PKCS1_OAEP.new(self.private_key, hashAlgo=SHA256)
        return cipher.decrypt(base64.b64decode(value))

    def _write_qr(self, fingerprint):
        url = f"https://discord.com/ra/{fingerprint}"
        cache = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "dsqrd")
        os.makedirs(cache, mode=0o700, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="login-", suffix=".png", dir=cache)
        os.close(fd)
        with open(path, "wb") as output:
            qrcode.make(url).save(output)
        os.chmod(path, 0o600)
        self.qr_path = path
        self.emit({"state": "qr", "qr": path})

    def _exchange(self, ticket):
        request = urllib.request.Request(
            LOGIN_URL,
            data=json.dumps({"ticket": ticket}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": self.user_agent},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            detail = "Discord rejected the QR login"
            try:
                if "captcha_key" in json.load(error):
                    detail = "Discord requires verification in the official client"
            except Exception:
                pass
            raise RuntimeError(detail) from error
        encrypted = payload.get("encrypted_token")
        if not encrypted:
            raise RuntimeError("Discord returned no login credential")
        return self._decrypt(encrypted).decode()

    def run(self):
        token = None
        heartbeat_at = None
        heartbeat_interval = 30.0
        deadline = time.monotonic() + 100
        public_key = base64.b64encode(self.private_key.public_key().export_key(format="DER")).decode()
        try:
            self.emit({"state": "connecting"})
            self.ws = websocket.create_connection(
                GATEWAY_URL,
                header=["Origin: https://discord.com", f"User-Agent: {self.user_agent}"],
                timeout=5,
                suppress_origin=True,
            )
            self.ws.settimeout(1)
            self.ws.send(json.dumps({"op": "init", "encoded_public_key": public_key}))
            while not self.cancelled.is_set() and time.monotonic() < deadline:
                if heartbeat_at is not None and time.monotonic() >= heartbeat_at:
                    self.ws.send(json.dumps({"op": "heartbeat"}))
                    heartbeat_at = time.monotonic() + heartbeat_interval
                try:
                    message = json.loads(self.ws.recv())
                except websocket.WebSocketTimeoutException:
                    self.emit({"state": "waiting", "remaining": max(0, int(deadline - time.monotonic()))})
                    continue
                op = message.get("op")
                if op == "hello":
                    heartbeat_interval = int(message.get("heartbeat_interval", 30000)) / 1000
                    deadline = time.monotonic() + int(message.get("timeout_ms", 100000)) / 1000
                    heartbeat_at = time.monotonic() + min(heartbeat_interval, 5)
                elif op == "nonce_proof":
                    nonce = self._decrypt(message["encrypted_nonce"])
                    proof = base64.urlsafe_b64encode(nonce).decode().rstrip("=")
                    self.ws.send(json.dumps({"op": "nonce_proof", "nonce": proof}))
                elif op == "pending_remote_init":
                    self._write_qr(message["fingerprint"])
                elif op == "pending_ticket":
                    fields = self._decrypt(message["encrypted_user_payload"]).decode().split(":")
                    self.emit({"state": "confirming", "account": fields[3] if len(fields) > 3 else "Discord account"})
                elif op == "pending_login":
                    token = self._exchange(message["ticket"])
                    self.emit({"state": "approved"})
                    break
                elif op == "cancel":
                    raise RuntimeError("QR login was cancelled in Discord")
            if self.cancelled.is_set():
                self.emit({"state": "signedOut"})
            elif not token and time.monotonic() >= deadline:
                raise RuntimeError("QR login timed out")
            return token
        except Exception as error:
            if not self.cancelled.is_set():
                self.emit({"state": "error", "message": str(error)})
            return None
        finally:
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass
            if self.qr_path:
                try:
                    os.unlink(self.qr_path)
                except FileNotFoundError:
                    pass
