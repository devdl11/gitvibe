from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

import test_accounts as accounts
from vgit_server import ssh
from vgit_server.store import APIError, Store


class SSHTests(unittest.TestCase):
    setUp = accounts.AccountTests.setUp
    request = accounts.AccountTests.request

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.key = Path(cls.directory.name) / "id_ed25519"
        cls.other = Path(cls.directory.name) / "other"
        for key in (cls.key, cls.other):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, capture_output=True)
        cls.public = Path(str(cls.key) + ".pub").read_text()
        cls.fingerprint = ssh.public_key(cls.public)[1]

    def add(self, actor="alice", public=None):
        return self.request("POST", "/api/user/ssh-keys", {"title": "Laptop", "public_key": public or self.public}, actor)

    def challenge(self, username="alice", fingerprint=None):
        return self.request("POST", "/api/auth/ssh/challenge", {"username": username, "fingerprint": fingerprint or self.fingerprint})

    def sign(self, challenge, key=None, namespace="vgit-login", message=None):
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key or self.key), "-n", namespace], input=message or challenge["message"], text=True, capture_output=True, check=True).stdout

    def finish(self, challenge, signature):
        return self.request("POST", "/api/auth/ssh/login", {"challenge_id": challenge["challenge_id"], "signature": signature})

    def test_real_signature_login_and_replay(self):
        status, key = self.add()
        self.assertEqual(status, 201)
        self.assertEqual(key["fingerprint"], self.fingerprint)
        self.assertEqual(self.request("GET", "/api/user/ssh-keys", actor="alice")[1]["keys"], [key])
        status, challenge = self.challenge()
        self.assertEqual(status, 200)
        signature = self.sign(challenge)
        status, session = self.finish(challenge, signature)
        self.assertEqual(status, 200, session)
        self.assertEqual(session["user"]["username"], "alice")
        self.assertEqual(self.request("GET", "/api/user", token=session["token"])[0], 200)
        self.assertEqual(self.finish(challenge, signature)[0], 401)
        self.server.store = Store(self.path)
        self.assertEqual(self.request("GET", "/api/user", token=session["token"])[0], 200)

    def test_wrong_signer_namespace_message_and_malformed_signature(self):
        self.add()
        for sign in (
            lambda c: self.sign(c, key=self.other),
            lambda c: self.sign(c, namespace="file"),
            lambda c: self.sign(c, message="different message"),
            lambda c: "not a signature",
        ):
            with self.subTest(sign=sign):
                status, challenge = self.challenge()
                self.assertEqual(status, 200)
                self.assertEqual(self.finish(challenge, sign(challenge))[0], 401)
                self.assertEqual(self.finish(challenge, self.sign(challenge))[0], 401)

    def test_expiry_and_cross_account_binding(self):
        self.add()
        for username in ("bob", "absent"):
            self.assertEqual(self.challenge(username)[0], 401)
        self.assertEqual(self.challenge(fingerprint="SHA256:absent")[0], 401)
        _, challenge = self.challenge()
        with self.store.connection() as db:
            db.execute("UPDATE ssh_challenges SET expires_at=0")
        self.assertEqual(self.finish(challenge, self.sign(challenge))[0], 401)

    def test_key_removal_revokes_only_its_sessions_and_challenges(self):
        _, key = self.add()
        _, challenge = self.challenge()
        _, session = self.finish(challenge, self.sign(challenge))
        _, pending = self.challenge()
        self.assertEqual(self.request("DELETE", f'/api/user/ssh-keys/{key["id"]}', {}, "bob")[0], 404)
        self.assertEqual(self.request("DELETE", f'/api/user/ssh-keys/{key["id"]}', {}, "alice")[0], 200)
        self.assertEqual(self.request("GET", "/api/user", token=session["token"])[0], 401)
        self.assertEqual(self.request("GET", "/api/user", actor="alice")[0], 200)
        self.assertEqual(self.finish(pending, self.sign(pending))[0], 401)
        self.assertEqual(self.challenge()[0], 401)
        self.assertEqual(self.add(actor="bob")[0], 201)  # fully revoked keys may be registered again

    def test_duplicates_validation_and_private_key_rejection(self):
        self.assertEqual(self.add()[0], 201)
        self.assertEqual(self.add(actor="bob")[0], 409)
        self.assertEqual(self.add(public=self.public.strip().split()[0] + " " + self.public.split()[1] + " different comment")[0], 409)
        for public in ("invalid", "ssh-ed25519 !!!", self.key.read_text(), self.public + self.public, "command=evil " + self.public):
            self.assertEqual(self.add(public=public)[0], 400)
        self.assertEqual(self.request("GET", "/api/user/ssh-keys")[0], 401)
        self.assertEqual(self.request("GET", "/api/user/ssh-keys", actor="bob")[1]["keys"], [])
        self.assertEqual(self.request("DELETE", "/api/user/ssh-keys/999", {}, "alice")[0], 404)

    def test_password_change_invalidates_pending_challenges(self):
        self.add()
        _, challenge = self.challenge()
        self.assertEqual(self.request("PUT", "/api/user/password", {"current_password": "correct horse battery", "new_password": "a new long password"}, "alice")[0], 200)
        self.assertEqual(self.finish(challenge, self.sign(challenge))[0], 401)
        self.assertEqual(self.challenge()[0], 200)  # registered keys remain usable

    def test_challenge_cap_and_http_rate_limit(self):
        self.add()
        for _ in range(10):
            self.assertEqual(self.challenge()[0], 200)
        self.assertEqual(self.challenge()[0], 429)
        for _ in range(2):
            self.server.auth_limiter.check("127.0.0.1")
        self.assertEqual(self.challenge()[0], 429)

    def test_additive_database_migration(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires_at INTEGER NOT NULL);
                INSERT INTO users VALUES(1,'legacy','salt','hash',0);
                INSERT INTO sessions VALUES('existing',1,9999999999);
            """)
            db.commit()
        store = Store(legacy)
        Store(legacy)  # startup is idempotent
        with store.connection() as db:
            session = db.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(session["token_hash"], "existing")
            self.assertIsNone(session["ssh_key_id"])

    def test_other_supported_key_types(self):
        for kind, options in (("rsa", ["-b", "3072"]), ("ecdsa", ["-b", "256"])):
            with self.subTest(kind=kind):
                path = Path(self.temp.name) / kind
                subprocess.run(["ssh-keygen", "-q", "-t", kind, *options, "-N", "", "-f", str(path)], check=True, capture_output=True)
                public = Path(str(path) + ".pub").read_text()
                status, key = self.add(public=public)
                self.assertEqual(status, 201, key)
                _, challenge = self.challenge(fingerprint=key["fingerprint"])
                self.assertEqual(self.finish(challenge, self.sign(challenge, key=path))[0], 200)

    def test_concurrent_replay_only_issues_one_session(self):
        self.add()
        _, challenge = self.challenge()
        payload = {"challenge_id": challenge["challenge_id"], "signature": self.sign(challenge)}

        def attempt():
            try:
                return self.store.dispatch("POST", ["auth", "ssh", "login"], payload)[0]
            except APIError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(results), [200, 401])
