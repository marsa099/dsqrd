import base64
import io
import json
from http.client import HTTPMessage
import os
import tempfile
import threading
import unittest
from unittest import mock

from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

import dsqrd as daemon
from dchat import remote_auth, token


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return json.dumps(self.payload).encode()


class FakeSocket:
    def __init__(self, token=b"approved-token"):
        self.token = token
        self.events = []
        self.sent = []

    def settimeout(self, _):
        pass

    def send(self, value):
        payload = json.loads(value)
        self.sent.append(payload)
        if payload.get("op") != "init":
            return
        public = RSA.import_key(base64.b64decode(payload["encoded_public_key"]))
        cipher = PKCS1_OAEP.new(public, hashAlgo=SHA256)
        encrypt = lambda value: base64.b64encode(cipher.encrypt(value)).decode()
        self.events = [
            {"op": "hello", "heartbeat_interval": 30000, "timeout_ms": 100000},
            {"op": "nonce_proof", "encrypted_nonce": encrypt(b"nonce")},
            {"op": "pending_remote_init", "fingerprint": "fingerprint"},
            {"op": "pending_ticket", "encrypted_user_payload": encrypt(b"123:0:avatar:david")},
            {"op": "pending_login", "ticket": "ticket"},
        ]
        self.encrypted_token = encrypt(self.token)

    def recv(self):
        return json.dumps(self.events.pop(0))

    def close(self):
        pass


class RemoteAuthTest(unittest.TestCase):
    def test_approved_qr_returns_credential_without_emitting_it(self):
        events = []
        socket = FakeSocket()
        with tempfile.TemporaryDirectory() as cache, \
             mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache}), \
             mock.patch.object(remote_auth.websocket, "create_connection", return_value=socket), \
             mock.patch.object(remote_auth.urllib.request, "urlopen", side_effect=lambda *_a, **_k: FakeResponse({"encrypted_token": socket.encrypted_token})):
            credential = remote_auth.RemoteAuth("test-agent", events.append).run()

        self.assertEqual(credential, "approved-token")
        self.assertEqual([e["state"] for e in events], ["connecting", "qr", "confirming", "approved"])
        self.assertTrue(all("token" not in event for event in events))
        proof = next(item for item in socket.sent if item.get("op") == "nonce_proof")
        self.assertEqual(proof["nonce"], base64.urlsafe_b64encode(b"nonce").decode().rstrip("="))

    def test_cancelled_session_returns_no_credential(self):
        events = []
        session = remote_auth.RemoteAuth("test-agent", events.append)
        session.cancel()
        with mock.patch.object(remote_auth.websocket, "create_connection", return_value=FakeSocket()):
            self.assertIsNone(session.run())
        self.assertEqual(events[-1]["state"], "signedOut")

    def test_timeout_is_retryable_error(self):
        events = []
        with mock.patch.object(remote_auth.websocket, "create_connection", return_value=FakeSocket()), \
             mock.patch.object(remote_auth.time, "monotonic", side_effect=[0, 101, 101]):
            self.assertIsNone(remote_auth.RemoteAuth("test-agent", events.append).run())
        self.assertEqual(events[-1], {"state": "error", "message": "QR login timed out"})

    def test_ticket_exchange_failure_emits_safe_error(self):
        events = []
        socket = FakeSocket()
        error = remote_auth.urllib.error.HTTPError(remote_auth.LOGIN_URL, 400, "bad", HTTPMessage(), io.BytesIO(b"{}"))
        with tempfile.TemporaryDirectory() as cache, \
             mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache}), \
             mock.patch.object(remote_auth.websocket, "create_connection", return_value=socket), \
             mock.patch.object(remote_auth.urllib.request, "urlopen", side_effect=error):
            self.assertIsNone(remote_auth.RemoteAuth("test-agent", events.append).run())
        self.assertEqual(events[-1]["state"], "error")
        self.assertNotIn("ticket", events[-1]["message"].lower())

    def test_keyring_save_replaces_selected_profile(self):
        stored = {"selected": "work", "profiles": [{"name": "work", "token": "old"}]}
        with mock.patch.object(token, "load_secret", return_value=json.dumps(stored)), \
             mock.patch.object(token.subprocess, "run") as run:
            token.save_secret("new")
        saved = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(saved["profiles"][0]["token"], "new")
        self.assertEqual(run.call_args.args[0][0:2], ["secret-tool", "store"])

    def test_connection_failure_becomes_retryable_auth_error(self):
        app = object.__new__(daemon.DQS)
        app.client_props = {}
        app.user_agent = "test-agent"
        app.discord = None
        app.gateway = None
        app.auth_status = {}
        app.conns = []
        app.lock = threading.Lock()
        with mock.patch.object(daemon.client_properties, "add_for_gateway", return_value={}), \
             mock.patch.object(daemon.client_properties, "encode_properties", return_value=""), \
             mock.patch.object(daemon.discord_mod, "Discord", side_effect=OSError("offline")):
            app._connect_discord("credential")
        self.assertEqual(app.auth_status, {
            "type": "auth", "state": "error", "message": "Could not connect to Discord",
        })
        self.assertIsNone(app.discord)
        self.assertIsNone(app.gateway)


if __name__ == "__main__":
    unittest.main()
