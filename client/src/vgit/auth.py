"""HTTP account login and SSH key management for the CLI."""

import getpass
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def api(server, method, path, data=None, token=None):
    parsed = urlsplit(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("--server must be an HTTP(S) server origin, such as https://vgit.example.com")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Use HTTPS for remote authentication; HTTP is supported on loopback only")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(server.rstrip("/") + "/api/" + path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            payload = json.load(error)
            message = payload.get("error", "Request failed")
            vibe = payload.get("vibe")
            if isinstance(vibe, str) and len(vibe) <= 500 and not any(ord(c) < 32 for c in vibe):
                message = f"{vibe} ({message})"
        except (ValueError, AttributeError):
            message = "Request failed"
        raise ValueError(f"Server returned HTTP {error.code}: {message}") from None
    if not isinstance(result, dict):
        raise ValueError("Server must return a JSON object")
    return result


def login(args):
    if args.ssh_key:
        key = str(Path(args.ssh_key).expanduser().absolute())
        # Fingerprinting reads the public half; an agent may hold the private half.
        public = key if key.endswith(".pub") else key + ".pub"
        result = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", public], text=True, capture_output=True, timeout=10)
        if result.returncode or len(result.stdout.split()) < 2:
            raise ValueError("Cannot read SSH public key; supply the matching .pub file beside the private key, or pass a .pub key available in ssh-agent")
        challenge = api(args.server, "POST", "auth/ssh/challenge", {"username": args.username, "fingerprint": result.stdout.split()[1]})
        if challenge.get("namespace") != "vgit-login" or not isinstance(challenge.get("message"), str) or len(challenge["message"]) > 1024:
            raise ValueError("Invalid SSH login challenge from server")
        signed = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key, "-n", "vgit-login"], input=challenge["message"], text=True, stdout=subprocess.PIPE, timeout=120)
        if signed.returncode:
            raise ValueError("SSH signing failed; check your key or ssh-agent")
        session = api(args.server, "POST", "auth/ssh/login", {"challenge_id": challenge["challenge_id"], "signature": signed.stdout})
    else:
        session = api(args.server, "POST", "auth/login", {"username": args.username, "password": getpass.getpass("Password: ")})
    # Explicit JSON output lets callers choose their credential storage.
    print(json.dumps(session))


def keys(args):
    token = os.environ.get("GITVIBE_TOKEN")
    if not token:
        raise ValueError("Set GITVIBE_TOKEN to your login token")
    if args.key_action == "add":
        result = api(args.server, "POST", "user/ssh-keys", {"title": args.title, "public_key": Path(args.public_key).expanduser().read_text(encoding="utf-8")}, token)
    elif args.key_action == "list":
        result = api(args.server, "GET", "user/ssh-keys", token=token)
    else:
        if not args.id.isascii() or not args.id.isdigit():
            raise ValueError("SSH key ID must be numeric")
        result = api(args.server, "DELETE", f"user/ssh-keys/{args.id}", {}, token)
    print(json.dumps(result))


def add_commands(commands):
    command = commands.add_parser("login", help="Log in with a password or SSH key; print session JSON")
    command.add_argument("--server", required=True)
    command.add_argument("--username", required=True)
    command.add_argument("--ssh-key", help="SSH private key, or public key backed by ssh-agent")
    command.set_defaults(run=login)
    command = commands.add_parser("ssh-key", help="Manage account SSH public keys")
    actions = command.add_subparsers(dest="key_action", required=True)
    for action in ("add", "list", "remove"):
        child = actions.add_parser(action)
        child.add_argument("--server", required=True)
        child.set_defaults(run=keys)
        if action == "add":
            child.add_argument("--public-key", required=True)
            child.add_argument("--title", required=True)
        elif action == "remove":
            child.add_argument("id")
