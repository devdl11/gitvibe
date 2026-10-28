import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vgit_server.app import AuthLimiter, Handler
from vgit_server.store import APIError, Store, token_hash


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "accounts.sqlite3"
        self.store = Store(self.path)
        self.server = SimpleNamespace(store=self.store, auth_limiter=AuthLimiter())
        self.tokens = {}
        for username in ("alice", "bob", "eve"):
            self.assertEqual(self.request("POST", "/api/accounts", {"username": username, "password": "correct horse battery"})[0], 201)
            status, session = self.request("POST", "/api/auth/login", {"username": username, "password": "correct horse battery"})
            self.assertEqual(status, 200)
            self.tokens[username] = session["token"]

    def request(self, method, path, data=None, actor=None, token=None, raw=None, extra_headers=""):
        body = json.dumps(data).encode() if data is not None else b""
        if raw is not None:
            body = raw
        token = token or self.tokens.get(actor)
        headers = f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n"
        if token:
            headers += f"Authorization: Bearer {token}\r\n"
        request = f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n{headers}{extra_headers}\r\n".encode() + body

        class Connection:
            def __init__(self):
                self.output = bytearray()

            def makefile(self, *args):
                return io.BytesIO(request)

            def sendall(self, data):
                self.output.extend(data)

        connection = Connection()
        with patch.object(Handler, "log_message"):
            Handler(connection, ("127.0.0.1", 12345), self.server)
        headers, body = bytes(connection.output).split(b"\r\n\r\n", 1)
        self.assertIn(b"Cache-Control: no-store", headers)
        return int(headers.split()[1]), json.loads(body)

    def create(self, visibility="private", actor="alice", repo="project"):
        status, result = self.request("POST", "/api/repos", {"name": repo, "visibility": visibility}, actor)
        self.assertEqual(status, 201, result)
        return result

    def grant(self, role, actor="alice", target="bob"):
        return self.request("PUT", f"/api/repos/alice/project/collaborators/{target}", {"role": role}, actor)

    def test_registration_login_validation_and_secret_storage(self):
        self.assertEqual(self.request("POST", "/api/accounts", {"username": "ALICE", "password": "correct horse battery"})[0], 409)
        for data in ({"username": "../evil", "password": "correct horse battery"}, {"username": "new", "password": "short"}, {"username": "new", "password": "correct horse battery", "id": 1}):
            self.assertEqual(self.request("POST", "/api/accounts", data)[0], 400)
        for username in ("alice", "absent"):
            status, error = self.request("POST", "/api/auth/login", {"username": username, "password": "wrong password"})
            self.assertEqual((status, error["error"]), (401, "Invalid username or password"))
            self.assertTrue(error["vibe"])
        self.assertEqual(self.request("GET", "/api/user")[0], 401)
        self.assertEqual(self.request("GET", "/api/user", token="bogus")[0], 401)
        status, user = self.request("GET", "/api/user", actor="alice")
        self.assertEqual(status, 200)
        self.assertEqual(set(user), {"id", "username", "created_at"})
        with self.store.connection() as db:
            account = db.execute("SELECT * FROM users WHERE username='alice'").fetchone()
            self.assertNotEqual(account["password_hash"], "correct horse battery")
            self.assertNotEqual(account["salt"], db.execute("SELECT salt FROM users WHERE username='bob'").fetchone()[0])
            session = db.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash(self.tokens["alice"]),)).fetchone()
            self.assertIsNotNone(session)
        contents = self.path.read_bytes()
        self.assertNotIn(b"correct horse battery", contents)
        self.assertNotIn(self.tokens["alice"].encode(), contents)

    def test_private_visibility_and_filtered_lists(self):
        self.create()
        for actor in (None, "bob", "eve"):
            self.assertEqual(self.request("GET", "/api/repos/alice/project", actor=actor)[0], 404)
            self.assertEqual(self.request("GET", "/api/users/alice/repos", actor=actor)[1]["repositories"], [])
        self.assertEqual(len(self.request("GET", "/api/user/repos", actor="alice")[1]["repositories"]), 1)
        self.assertEqual(self.request("POST", "/api/repos", {"name": "project"}, "alice")[0], 409)
        self.create(actor="bob")  # namespaces are independent
        self.assertEqual(self.request("POST", "/api/repos", {"name": "unauthorized"})[0], 401)

    def test_role_changes_revocation_and_write_gate(self):
        self.create()
        for role in ("read", "write", "admin"):
            self.assertEqual(self.grant(role)[0], 200)
            status, repo = self.request("GET", "/api/repos/alice/project", actor="bob")
            self.assertEqual((status, repo["permissions"]), (200, role))
            self.assertEqual(len(self.request("GET", "/api/user/repos", actor="bob")[1]["repositories"]), 1)
            if role != "admin":
                self.assertEqual(self.grant("admin", actor="bob")[0], 403)
                self.assertEqual(self.request("GET", "/api/repos/alice/project/collaborators", actor="bob")[0], 403)
            with self.store.connection() as db:
                actor = self.store.authenticate(db, self.tokens["bob"])
                row = db.execute(self.store.repo_query() + " WHERE r.id=?", (repo["id"],)).fetchone()
                if role == "read":
                    with self.assertRaises(APIError) as error:
                        self.store.require_access(db, row, actor, "write")
                    self.assertEqual(error.exception.status, 403)
                else:
                    self.assertEqual(self.store.require_access(db, row, actor, "write"), role)
            self.assertEqual(self.request("PATCH", "/api/repos/alice/project", {"visibility": "public"}, "bob")[0], 403)
            self.assertEqual(self.request("DELETE", "/api/repos/alice/project", {"confirm": "alice/project"}, "bob")[0], 403)
        self.assertEqual(self.grant("read", actor="bob", target="eve")[0], 200)
        self.assertEqual(self.grant("read", actor="bob", target="alice")[0], 403)
        self.assertEqual(self.grant("owner")[0], 400)
        self.assertEqual(self.grant("read", target="absent")[0], 404)
        self.assertEqual(self.request("DELETE", "/api/repos/alice/project/collaborators/bob", actor="alice")[0], 200)
        self.assertEqual(self.request("GET", "/api/repos/alice/project", actor="bob")[0], 404)
        self.assertEqual(self.grant("admin", actor="eve")[0], 403)

    def test_public_visibility_changes_and_delete_cascade(self):
        self.create("public")
        status, repo = self.request("GET", "/api/repos/alice/project")
        self.assertEqual((status, repo["permissions"]), (200, "read"))
        self.assertEqual(len(self.request("GET", "/api/users/alice/repos")[1]["repositories"]), 1)
        self.assertEqual(self.request("GET", "/api/repos/alice/project/collaborators", actor="eve")[0], 403)
        self.assertEqual(self.request("GET", "/api/repos/alice/project/collaborators")[0], 401)
        self.grant("write")
        self.assertEqual(self.request("DELETE", "/api/repos/alice/project/collaborators/bob", actor="alice")[0], 200)
        self.assertEqual(self.request("GET", "/api/repos/alice/project", actor="bob")[1]["permissions"], "read")
        self.assertEqual(self.request("PATCH", "/api/repos/alice/project", {"visibility": "private"}, "alice")[0], 200)
        self.assertEqual(self.request("GET", "/api/repos/alice/project")[0], 404)
        self.grant("read")
        self.assertEqual(self.request("DELETE", "/api/repos/alice/project", {"confirm": "wrong"}, "alice")[0], 400)
        self.assertEqual(self.request("DELETE", "/api/repos/alice/project", {"confirm": "alice/project"}, "alice")[0], 200)
        self.assertEqual(self.request("GET", "/api/repos/alice/project", actor="alice")[0], 404)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collaborators").fetchone()[0], 0)

    def test_logout_expiry_and_password_change(self):
        self.assertEqual(self.request("POST", "/api/auth/logout", {}, "bob")[0], 200)
        self.assertEqual(self.request("GET", "/api/user", actor="bob")[0], 401)
        with self.store.connection() as db:
            db.execute("UPDATE sessions SET expires_at=0 WHERE token_hash=?", (token_hash(self.tokens["eve"]),))
        self.assertEqual(self.request("GET", "/api/user", actor="eve")[0], 401)
        second = self.request("POST", "/api/auth/login", {"username": "alice", "password": "correct horse battery"})[1]["token"]
        self.assertEqual(self.request("PUT", "/api/user/password", {"current_password": "wrong password", "new_password": "a new long password"}, "alice")[0], 401)
        self.assertEqual(self.request("PUT", "/api/user/password", {"current_password": "correct horse battery", "new_password": "a new long password"}, "alice")[0], 200)
        self.assertEqual(self.request("GET", "/api/user", actor="alice")[0], 401)
        self.assertEqual(self.request("GET", "/api/user", token=second)[0], 401)
        self.assertEqual(self.request("POST", "/api/auth/login", {"username": "alice", "password": "correct horse battery"})[0], 401)
        self.assertEqual(self.request("POST", "/api/auth/login", {"username": "alice", "password": "a new long password"})[0], 200)

    def test_persistence_across_store_instances(self):
        self.create()
        self.grant("write")
        self.server.store = Store(self.path)
        self.assertEqual(self.request("GET", "/api/repos/alice/project", actor="bob")[1]["permissions"], "write")

    def test_http_validation_and_auth_throttle(self):
        self.assertEqual(self.request("POST", "/api/accounts", raw=b"{")[0], 400)
        self.assertEqual(self.request("POST", "/api/accounts", raw=b"[]")[0], 400)
        self.assertEqual(self.request("POST", "/api/accounts", raw=b"x" * 16385)[0], 413)
        self.assertEqual(self.request("GET", "/api/user?token=bad", actor="alice")[0], 400)
        self.assertEqual(self.request("GET", "/api/user", extra_headers="Authorization: Basic bad\r\n")[0], 401)
        self.assertEqual(self.request("GET", "/api/user", actor="alice", extra_headers="Authorization: Bearer bad\r\n")[0], 401)
        self.assertEqual(self.request("POST", "/api/accounts", extra_headers="Content-Length: 1\r\n")[0], 400)
        self.assertEqual(self.request("POST", "/api/accounts", extra_headers="Transfer-Encoding: chunked\r\n")[0], 400)
        limiter = self.server.auth_limiter
        for _ in range(14):  # setUp already made six expensive auth requests
            limiter.check("127.0.0.1")
        self.assertEqual(self.request("POST", "/api/auth/login", {"username": "alice", "password": "correct horse battery"})[0], 429)
        # Existing sessions still work while password endpoints are throttled.
        self.assertEqual(self.request("GET", "/api/user", actor="alice")[0], 200)
        with patch("vgit_server.app.time.monotonic", return_value=10**12):
            limiter.check("127.0.0.1")

    def test_invalid_repository_and_collaborator_fields(self):
        self.assertEqual(self.request("POST", "/api/repos", {"name": "private-default"}, "alice")[1]["visibility"], "private")
        self.create()
        for settings in ({"visibility": "internal"}, {"visibility": []}, {"description": 42}, {"description": "x" * 1001}, {"owner_id": 2}):
            self.assertEqual(self.request("PATCH", "/api/repos/alice/project", settings, "alice")[0], 400)
        for role in ([], None, "owner", "superuser"):
            self.assertEqual(self.grant(role)[0], 400)
        self.assertEqual(self.request("GET", "/api/repos/alice/project", actor="alice")[1]["visibility"], "private")


if __name__ == "__main__":
    unittest.main()
