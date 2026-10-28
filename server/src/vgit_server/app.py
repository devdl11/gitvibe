"""HTTP API for accounts and repository access management."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import unquote, urlsplit

from .store import APIError, Store
from .digest import DigestConfig, DigestError
from . import messages


class AuthLimiter:
    """Bound authentication operations per direct peer, independent of headers."""

    def __init__(self):
        self.attempts = {}
        self.lock = threading.Lock()

    def check(self, peer):
        now = time.monotonic()
        with self.lock:
            self.attempts = {key: value for key, value in self.attempts.items() if now - value[0] < 60}
            start, count = self.attempts.get(peer, (now, 0))
            if count >= 20 or (peer not in self.attempts and len(self.attempts) >= 4096):
                raise APIError(429, "Too many authentication requests; retry in 60 seconds")
            self.attempts[peer] = (start, count + 1)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_api()

    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET

    def handle_api(self):
        try:
            if self.command == "GET" and self.path == "/health":
                status, payload = 200, {"status": "ok", "service": "vgit-server", "version": "0.1.0"}
            else:
                parsed = urlsplit(self.path)
                if parsed.query or parsed.fragment:
                    raise APIError(400, "Query parameters are not supported")
                parts = unquote(parsed.path).strip("/").split("/")
                if not parts or parts.pop(0) != "api":
                    raise APIError(404, "Endpoint not found")
                if self.headers.get("Transfer-Encoding"):
                    raise APIError(400, "Transfer-Encoding is not supported")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) > 1:
                    raise APIError(400, "Ambiguous Content-Length")
                try:
                    length = int(lengths[0]) if lengths else 0
                except ValueError:
                    raise APIError(400, "Invalid Content-Length") from None
                is_revision_upload = self.command == "POST" and len(parts) == 4 and parts[0] == "repos" and parts[3] == "revisions"
                limit = 2_100_000 if is_revision_upload else 16_384
                if length < 0 or length > limit:
                    raise APIError(413, "Request body exceeds limit")
                data = {}
                if length:
                    if self.headers.get_content_type() != "application/json":
                        raise APIError(415, "Content-Type must be application/json")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise APIError(400, "Incomplete request body")
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise APIError(400, "Invalid JSON body") from None
                    if not isinstance(data, dict):
                        raise APIError(400, "Request body must be a JSON object")
                authorization = self.headers.get_all("Authorization", [])
                token = None
                if authorization:
                    if len(authorization) != 1:
                        raise APIError(401, "Invalid Authorization header")
                    credentials = authorization[0].split()
                    if len(credentials) != 2 or credentials[0].lower() != "bearer" or len(credentials[1]) > 256:
                        raise APIError(401, "Use Authorization: Bearer TOKEN")
                    token = credentials[1]
                if self.command in {"POST", "PUT"} and (parts in (["accounts"], ["auth", "login"], ["user", "password"], ["user", "ssh-keys"]) or parts[:2] == ["auth", "ssh"]):
                    self.server.auth_limiter.check(self.client_address[0])
                if self.command == "GET" and len(parts) in {5, 6} and parts[0] == "repos" and parts[3] == "revisions" and parts[-1] == "stream":
                    if data:
                        raise APIError(400, "Request body is not supported")
                    _, revision = self.server.store.dispatch("GET", parts[:-1], {}, token)
                    config = getattr(self.server, "digest_config", None)
                    if config is None:
                        raise APIError(503, "Server model is not configured")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        self.wfile.write((json.dumps({"type": "revision", "id": revision["id"]}) + "\n").encode())
                        self.wfile.flush()
                        for file in config.stream_files(revision["knowledge"]):
                            self.wfile.write((json.dumps({"type": "file", **file}) + "\n").encode())
                            self.wfile.flush()
                        self.wfile.write((json.dumps({'type': 'done', 'vibe': messages.render(config, 200)}) + '\n').encode())
                        self.wfile.flush()
                    except DigestError:
                        self.wfile.write((json.dumps({'type': 'error', 'error': 'Model reconstruction failed', 'vibe': messages.render(config, 503)}) + '\n').encode())
                        self.wfile.flush()
                    except (OSError, BrokenPipeError):
                        pass
                    return
                status, payload = self.server.store.dispatch(self.command, parts, data, token)
        except APIError as error:
            status, payload = error.status, {"error": str(error)}
        except (sqlite3.Error, OSError):
            logging.exception("Server storage failure")
            status, payload = 503, {"error": "Storage temporarily unavailable"}
        except (ValueError, RecursionError):
            status, payload = 400, {"error": "Invalid request"}
        if status >= 400 and isinstance(payload, dict):
            payload["vibe"] = messages.render(getattr(self.server, "digest_config", None), status)
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if status == 429:
            self.send_header("Retry-After", "60")
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, digest_config=None):
        self.store = store
        self.digest_config = digest_config
        self.auth_limiter = AuthLimiter()
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


def main():
    parser = argparse.ArgumentParser(prog="vgit-server", description="Custom GitVibe server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7848)
    parser.add_argument("--data-dir", type=Path, default=Path("vgit-data"))
    args = parser.parse_args()
    try:
        args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest_config = DigestConfig.from_env()
        store = Store(args.data_dir / "accounts.sqlite3")
        with Server((args.host, args.port), store, digest_config) as server:
            print(f"vgit-server listening on http://{args.host}:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except (OSError, OverflowError, sqlite3.Error, DigestError) as error:
        parser.exit(1, f"vgit-server: {error}\n")
