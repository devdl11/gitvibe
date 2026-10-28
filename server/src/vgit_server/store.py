"""Persistent accounts and repository authorization, independent of HTTP."""

from contextlib import contextmanager
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
import uuid

from . import ssh


class APIError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


ROLES = {"read": 1, "write": 2, "admin": 3, "owner": 4}
SESSION_SECONDS = 7 * 24 * 3600
NAME = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,37}[a-z0-9])?")


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value.lower()):
        raise APIError(400, "Names must contain 1–39 letters, digits, hyphens or underscores; start and end with a letter or digit")
    return value.lower()


def password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 256:
        raise APIError(400, "Passwords must contain 12–256 characters")
    return value


def derive(value, salt):
    return hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64 * 1024 * 1024).hex()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def fields(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise APIError(400, "Missing required fields or unsupported fields")


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repositories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL CHECK(visibility IN ('public', 'private')),
                    created_at INTEGER NOT NULL, UNIQUE(owner_id, name)
                );
                CREATE TABLE IF NOT EXISTS collaborators (
                    repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('read', 'write', 'admin')),
                    PRIMARY KEY(repo_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT NOT NULL, repo_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                    parent TEXT, knowledge TEXT NOT NULL,
                    PRIMARY KEY(repo_id, id)
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS collaborators_user ON collaborators(user_id);
                CREATE TABLE IF NOT EXISTS ssh_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, public_key TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ssh_challenges (
                    id TEXT PRIMARY KEY,
                    key_id INTEGER NOT NULL REFERENCES ssh_keys(id) ON DELETE CASCADE,
                    message TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
            """)
            # Additive migration for databases created before SSH authentication.
            if "ssh_key_id" not in {r["name"] for r in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN ssh_key_id INTEGER REFERENCES ssh_keys(id) ON DELETE CASCADE")

    def session(self, db, account, key_id=None):
        session = secrets.token_urlsafe(32)
        expires = int(time.time()) + SESSION_SECONDS
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
        db.execute("INSERT INTO sessions(token_hash,user_id,expires_at,ssh_key_id) VALUES(?,?,?,?)", (token_hash(session), account["id"], expires, key_id))
        return 200, {"token": session, "token_type": "Bearer", "expires_at": expires, "user": self.public_user(account)}

    def ssh_dispatch(self, db, method, parts, data, user):
        if parts == ["user", "ssh-keys"]:
            self.require_user(user)
            if method == "GET":
                keys = db.execute("SELECT id,title,public_key,fingerprint,created_at FROM ssh_keys WHERE user_id=? ORDER BY id", (user["id"],)).fetchall()
                return 200, {"keys": [dict(key) for key in keys]}
            if method == "POST":
                fields(data, {"title", "public_key"}, {"title", "public_key"})
                if not isinstance(data["title"], str) or not 1 <= len(data["title"].strip()) <= 100:
                    raise APIError(400, "Key title must contain 1–100 characters")
                try:
                    key, fingerprint = ssh.public_key(data["public_key"])
                except ssh.InvalidKey as error:
                    raise APIError(400, str(error)) from None
                if db.execute("SELECT count(*) FROM ssh_keys WHERE user_id=?", (user["id"],)).fetchone()[0] >= 50:
                    raise APIError(400, "Account SSH key limit reached")
                try:
                    cursor = db.execute("INSERT INTO ssh_keys(user_id,title,public_key,fingerprint,created_at) VALUES(?,?,?,?,?)", (user["id"], data["title"].strip(), key, fingerprint, int(time.time())))
                except sqlite3.IntegrityError:
                    raise APIError(409, "SSH key is already registered") from None
                key = db.execute("SELECT id,title,public_key,fingerprint,created_at FROM ssh_keys WHERE id=?", (cursor.lastrowid,)).fetchone()
                return 201, dict(key)
        if len(parts) == 3 and parts[:2] == ["user", "ssh-keys"] and method == "DELETE":
            self.require_user(user)
            fields(data, set())
            if not parts[2].isascii() or not parts[2].isdigit() or len(parts[2]) > 18:
                raise APIError(404, "SSH key not found")
            deleted = db.execute("DELETE FROM ssh_keys WHERE id=? AND user_id=?", (int(parts[2]), user["id"]))
            if not deleted.rowcount:
                raise APIError(404, "SSH key not found")
            return 200, {"message": "SSH key, pending challenges and sessions created with it revoked"}
        if parts == ["auth", "ssh", "challenge"] and method == "POST":
            fields(data, {"username", "fingerprint"}, {"username", "fingerprint"})
            if not isinstance(data["fingerprint"], str) or len(data["fingerprint"]) > 100:
                raise APIError(400, "Invalid key fingerprint")
            key = db.execute("SELECT k.id FROM ssh_keys k JOIN users u ON u.id=k.user_id WHERE u.username=? AND k.fingerprint=?", (name(data["username"]), data["fingerprint"])).fetchone()
            if key is None:
                raise APIError(401, "SSH authentication failed")
            now = int(time.time())
            db.execute("DELETE FROM ssh_challenges WHERE expires_at<=?", (now,))
            if db.execute("SELECT count(*) FROM ssh_challenges WHERE key_id=?", (key["id"],)).fetchone()[0] >= 10:
                raise APIError(429, "Too many pending challenges; wait for expiry")
            challenge = secrets.token_urlsafe(32)
            message = f"vgit SSH login\nChallenge: {challenge}\n"
            expires = now + 120
            db.execute("INSERT INTO ssh_challenges VALUES(?,?,?,?)", (challenge, key["id"], message, expires))
            return 200, {"challenge_id": challenge, "message": message, "namespace": ssh.NAMESPACE, "expires_at": expires}
        if parts == ["auth", "ssh", "login"] and method == "POST":
            fields(data, {"challenge_id", "signature"}, {"challenge_id", "signature"})
            if not isinstance(data["challenge_id"], str) or len(data["challenge_id"]) > 100 or not isinstance(data["signature"], str) or len(data["signature"]) > 12000:
                raise APIError(400, "Invalid SSH challenge or signature")
            challenge = db.execute("SELECT c.*, k.public_key, k.user_id FROM ssh_challenges c JOIN ssh_keys k ON k.id=c.key_id WHERE c.id=? AND c.expires_at>?", (data["challenge_id"], int(time.time()))).fetchone()
            if challenge is None:
                raise APIError(401, "SSH authentication failed")
            valid = ssh.verify(challenge["public_key"], challenge["message"], data["signature"]) and int(time.time()) < challenge["expires_at"]
            db.execute("DELETE FROM ssh_challenges WHERE id=?", (challenge["id"],))
            if not valid:
                # Return rather than raise so the failed attempt still consumes its nonce.
                return 401, {"error": "SSH authentication failed"}
            account = db.execute("SELECT * FROM users WHERE id=?", (challenge["user_id"],)).fetchone()
            return self.session(db, account, challenge["key_id"])
        raise APIError(404, "Endpoint not found")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def authenticate(self, db, token):
        if token is None:
            return None
        row = db.execute("""SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
                            WHERE s.token_hash=? AND s.expires_at>?""", (token_hash(token), int(time.time()))).fetchone()
        if row is None:
            raise APIError(401, "Invalid or expired token")
        return row

    @staticmethod
    def require_user(user):
        if user is None:
            raise APIError(401, "Authentication required")
        return user

    @staticmethod
    def public_user(user):
        return {k: user[k] for k in ("id", "username", "created_at")}

    def access(self, db, repo, user):
        if user is not None and repo["owner_id"] == user["id"]:
            return "owner"
        member = db.execute("SELECT role FROM collaborators WHERE repo_id=? AND user_id=?", (repo["id"], user["id"])).fetchone() if user else None
        return member["role"] if member else ("read" if repo["visibility"] == "public" else None)

    def require_access(self, db, repo, user, required="read"):
        """Use this same gate for future fetch/pull (read) and push (write)."""
        role = self.access(db, repo, user)
        if role is None:
            raise APIError(404, "Repository not found")
        if ROLES[role] < ROLES[required]:
            raise APIError(403 if user else 401, f"Repository {required} permission required")
        return role

    def serialize_repo(self, db, repo, user):
        return {**dict(repo), "permissions": self.access(db, repo, user), "full_name": f'{repo["owner"]}/{repo["name"]}'}

    @staticmethod
    def repo_query():
        return "SELECT r.*, u.username AS owner FROM repositories r JOIN users u ON u.id=r.owner_id"

    def dispatch(self, method, parts, data, token=None):
        """Return (HTTP status, JSON body). All checks and mutations share a transaction."""
        with self.connection() as db:
            # Serialize writes before permission checks to prevent concurrent revocation races.
            if method != "GET":
                db.execute("BEGIN IMMEDIATE")
            user = self.authenticate(db, token)
            if parts[:2] in (["user", "ssh-keys"], ["auth", "ssh"]):
                return self.ssh_dispatch(db, method, parts, data, user)
            if parts == ["accounts"] and method == "POST":
                fields(data, {"username", "password"}, {"username", "password"})
                username, secret = name(data["username"]), password(data["password"])
                salt = secrets.token_hex(16)
                try:
                    cursor = db.execute("INSERT INTO users(username,salt,password_hash,created_at) VALUES(?,?,?,?)", (username, salt, derive(secret, salt), int(time.time())))
                except sqlite3.IntegrityError:
                    raise APIError(409, "Username already exists") from None
                created = db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
                return 201, self.public_user(created)
            if parts == ["auth", "login"] and method == "POST":
                fields(data, {"username", "password"}, {"username", "password"})
                username = name(data["username"])
                secret = data["password"]
                if not isinstance(secret, str) or len(secret) > 256:
                    raise APIError(401, "Invalid username or password")
                account = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                # Perform the expensive derivation even when the username does not exist.
                calculated = derive(secret, account["salt"] if account else "00" * 16)
                if account is None or not hmac.compare_digest(calculated, account["password_hash"]):
                    raise APIError(401, "Invalid username or password")
                return self.session(db, account)
            if parts == ["auth", "logout"] and method == "POST":
                self.require_user(user)
                fields(data, set())
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
                return 200, {"message": "Logged out"}
            if parts == ["user"] and method == "GET":
                return 200, self.public_user(self.require_user(user))
            if parts == ["user", "password"] and method == "PUT":
                self.require_user(user)
                fields(data, {"current_password", "new_password"}, {"current_password", "new_password"})
                current, new = password(data["current_password"]), password(data["new_password"])
                if not hmac.compare_digest(derive(current, user["salt"]), user["password_hash"]):
                    raise APIError(401, "Invalid current password")
                salt = secrets.token_hex(16)
                db.execute("UPDATE users SET salt=?, password_hash=? WHERE id=?", (salt, derive(new, salt), user["id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
                db.execute("DELETE FROM ssh_challenges WHERE key_id IN (SELECT id FROM ssh_keys WHERE user_id=?)", (user["id"],))
                return 200, {"message": "Password changed; all sessions revoked"}
            if parts == ["repos"] and method == "POST":
                self.require_user(user)
                fields(data, {"name", "description", "visibility"}, {"name"})
                repo_name = name(data["name"])
                visibility, description = self.repo_settings(data)
                try:
                    cursor = db.execute("INSERT INTO repositories(owner_id,name,description,visibility,created_at) VALUES(?,?,?,?,?)", (user["id"], repo_name, description, visibility, int(time.time())))
                except sqlite3.IntegrityError:
                    raise APIError(409, "Repository already exists") from None
                repo = db.execute(self.repo_query() + " WHERE r.id=?", (cursor.lastrowid,)).fetchone()
                return 201, self.serialize_repo(db, repo, user)
            if parts == ["user", "repos"] and method == "GET":
                self.require_user(user)
                rows = db.execute(self.repo_query() + " WHERE r.owner_id=? OR EXISTS(SELECT 1 FROM collaborators c WHERE c.repo_id=r.id AND c.user_id=?) ORDER BY r.id", (user["id"], user["id"])).fetchall()
                return 200, {"repositories": [self.serialize_repo(db, r, user) for r in rows]}
            if len(parts) == 3 and parts[0] == "users" and parts[2] == "repos" and method == "GET":
                owner = name(parts[1])
                if not db.execute("SELECT 1 FROM users WHERE username=?", (owner,)).fetchone():
                    raise APIError(404, "User not found")
                rows = db.execute(self.repo_query() + " WHERE u.username=? ORDER BY r.id", (owner,)).fetchall()
                return 200, {"repositories": [self.serialize_repo(db, r, user) for r in rows if self.access(db, r, user)]}
            if len(parts) >= 3 and parts[0] == "repos":
                owner, repo_name = name(parts[1]), name(parts[2])
                repo = db.execute(self.repo_query() + " WHERE u.username=? AND r.name=?", (owner, repo_name)).fetchone()
                if repo is None:
                    raise APIError(404, "Repository not found")
                self.require_access(db, repo, user)
                if len(parts) in {4, 5} and parts[3] == "revisions":
                    if len(parts) == 4 and method == "POST":
                        self.require_access(db, repo, user, "write")
                        fields(data, {"knowledge"}, {"knowledge"})
                        knowledge = data["knowledge"]
                        if not isinstance(knowledge, str) or not knowledge.strip() or len(knowledge.encode()) > 2_000_000 or "```" in knowledge or "~~~" in knowledge:
                            raise APIError(400, "Invalid knowledge")
                        parent = db.execute("SELECT id FROM revisions WHERE repo_id=? ORDER BY id DESC LIMIT 1", (repo["id"],)).fetchone()
                        revision = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
                        db.execute("INSERT INTO revisions (id,repo_id,parent,knowledge) VALUES (?,?,?,?)", (revision, repo["id"], parent["id"] if parent else None, knowledge.strip()))
                        return 201, {"id": revision, "parent": parent["id"] if parent else None}
                    if method == "GET":
                        if len(parts) == 4:
                            row = db.execute("SELECT id,parent,knowledge FROM revisions WHERE repo_id=? ORDER BY id DESC LIMIT 1", (repo["id"],)).fetchone()
                        else:
                            row = db.execute("SELECT id,parent,knowledge FROM revisions WHERE repo_id=? AND id=?", (repo["id"], parts[4])).fetchone()
                        if row is None:
                            raise APIError(404, "Revision not found")
                        return 200, dict(row)
                if len(parts) == 3:
                    if method == "GET":
                        return 200, self.serialize_repo(db, repo, user)
                    if method in {"PATCH", "DELETE"}:
                        self.require_access(db, repo, user, "owner")
                        if method == "DELETE":
                            fields(data, {"confirm"}, {"confirm"})
                            if data["confirm"] != f"{owner}/{repo_name}":
                                raise APIError(400, "Confirm deletion with the repository full_name")
                            db.execute("DELETE FROM repositories WHERE id=?", (repo["id"],))
                            return 200, {"message": "Repository deleted"}
                        fields(data, {"description", "visibility"})
                        visibility, description = self.repo_settings(data, repo)
                        db.execute("UPDATE repositories SET visibility=?, description=? WHERE id=?", (visibility, description, repo["id"]))
                        updated = db.execute(self.repo_query() + " WHERE r.id=?", (repo["id"],)).fetchone()
                        return 200, self.serialize_repo(db, updated, user)
                if len(parts) in {4, 5} and parts[3] == "collaborators":
                    self.require_access(db, repo, user, "admin")
                    if len(parts) == 4 and method == "GET":
                        rows = db.execute("SELECT u.username, c.role FROM collaborators c JOIN users u ON u.id=c.user_id WHERE c.repo_id=? ORDER BY u.username", (repo["id"],)).fetchall()
                        return 200, {"owner": owner, "collaborators": [dict(r) for r in rows]}
                    if len(parts) == 5 and method in {"PUT", "DELETE"}:
                        target = db.execute("SELECT id FROM users WHERE username=?", (name(parts[4]),)).fetchone()
                        if target is None:
                            raise APIError(404, "User not found")
                        if target["id"] == repo["owner_id"]:
                            raise APIError(403, "The owner's access cannot be changed")
                        if method == "DELETE":
                            fields(data, set())
                            db.execute("DELETE FROM collaborators WHERE repo_id=? AND user_id=?", (repo["id"], target["id"]))
                            return 200, {"message": "Collaborator access removed"}
                        fields(data, {"role"}, {"role"})
                        if not isinstance(data["role"], str) or data["role"] not in {"read", "write", "admin"}:
                            raise APIError(400, "Role must be read, write or admin")
                        db.execute("INSERT INTO collaborators VALUES(?,?,?) ON CONFLICT(repo_id,user_id) DO UPDATE SET role=excluded.role", (repo["id"], target["id"], data["role"]))
                        return 200, {"username": name(parts[4]), "role": data["role"]}
            raise APIError(404, "Endpoint not found")

    @staticmethod
    def repo_settings(data, previous=None):
        visibility = data.get("visibility", previous["visibility"] if previous else "private")
        description = data.get("description", previous["description"] if previous else "")
        if not isinstance(visibility, str) or visibility not in {"public", "private"}:
            raise APIError(400, "Visibility must be public or private")
        if not isinstance(description, str) or len(description) > 1000:
            raise APIError(400, "Description must be a string of at most 1000 characters")
        return visibility, description
