import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vgit import auth


class AuthTests(unittest.TestCase):
    def test_cli_signs_the_exact_challenge_with_real_ssh_key(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "identity"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, capture_output=True)
            message = "vgit SSH login\nChallenge: test-nonce\n"
            challenge = {"challenge_id": "test-nonce", "namespace": "vgit-login", "message": message}
            args = SimpleNamespace(server="http://localhost:7848", username="alice", ssh_key=str(key))
            output = io.StringIO()
            with patch.object(auth, "api", side_effect=[challenge, {"token": "session"}]) as api, contextlib.redirect_stdout(output):
                auth.login(args)
            self.assertEqual(json.loads(output.getvalue()), {"token": "session"})
            first = api.call_args_list[0].args
            self.assertEqual(first[:3], (args.server, "POST", "auth/ssh/challenge"))
            self.assertEqual(first[3]["username"], "alice")
            self.assertTrue(first[3]["fingerprint"].startswith("SHA256:"))
            payload = api.call_args_list[1].args[3]
            self.assertEqual(payload["challenge_id"], "test-nonce")
            signature = Path(directory) / "signature"
            signature.write_text(payload["signature"])
            allowed = Path(directory) / "allowed"
            allowed.write_text("vgit " + Path(str(key) + ".pub").read_text())
            result = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "vgit", "-n", "vgit-login", "-s", str(signature)], input=message, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_wrong_namespace_before_signing(self):
        args = SimpleNamespace(server="http://localhost", username="alice", ssh_key="/tmp/identity")
        fingerprint = SimpleNamespace(returncode=0, stdout="256 SHA256:test comment")
        with patch.object(auth.subprocess, "run", return_value=fingerprint) as process, patch.object(auth, "api", return_value={"namespace": "file", "message": "anything"}):
            with self.assertRaises(ValueError):
                auth.login(args)
            self.assertEqual(process.call_count, 1)

    def test_password_login_and_key_management(self):
        args = SimpleNamespace(server="http://localhost", username="alice", ssh_key=None)
        with patch.object(auth.getpass, "getpass", return_value="secret"), patch.object(auth, "api", return_value={"token": "session"}) as api, contextlib.redirect_stdout(io.StringIO()):
            auth.login(args)
        self.assertEqual(api.call_args.args[3], {"username": "alice", "password": "secret"})
        args = SimpleNamespace(server="http://localhost", key_action="list")
        with patch.dict(os.environ, {"GITVIBE_TOKEN": "session"}), patch.object(auth, "api", return_value={"keys": []}) as api, contextlib.redirect_stdout(io.StringIO()):
            auth.keys(args)
        self.assertEqual(api.call_args.kwargs["token"], "session")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                auth.keys(args)

    def test_invalid_server_origins_and_remote_plaintext_rejected(self):
        for server in ("http://example.com", "https://user:pass@example.com", "https://example.com?token=x", "https://example.com/path", "file:///tmp/file"):
            with self.subTest(server=server), patch.object(auth, "build_opener") as opener:
                with self.assertRaises(ValueError):
                    auth.api(server, "POST", "auth/login", {})
                opener.assert_not_called()

    def test_bearer_requests_and_redirect_refusal(self):
        response = io.BytesIO(b'{"keys":[]}')
        with patch.object(auth, "build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(auth.api("https://example.com", "GET", "user/ssh-keys", token="session"), {"keys": []})
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/api/user/ssh-keys")
        self.assertEqual(request.get_header("Authorization"), "Bearer session")
        self.assertIsInstance(opener.call_args.args[0], auth.NoRedirect)
        self.assertIsNone(auth.NoRedirect().redirect_request())
