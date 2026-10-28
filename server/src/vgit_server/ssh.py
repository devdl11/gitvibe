"""Verify OpenSSH public keys and SSHSIG signatures using ssh-keygen."""

import base64
import binascii
from pathlib import Path
import subprocess
import tempfile

NAMESPACE = "vgit-login"
KEY_TYPES = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}


class InvalidKey(ValueError):
    pass


def run(arguments, **kwargs):
    try:
        return subprocess.run(["ssh-keygen", *arguments], capture_output=True, timeout=10, **kwargs)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise OSError("SSH authentication requires a working OpenSSH ssh-keygen with SSHSIG support") from error


def public_key(value):
    if not isinstance(value, str) or len(value) > 8192 or "\n" in value.strip() or "\r" in value.strip():
        raise InvalidKey("Provide one OpenSSH public key line")
    parts = value.split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise InvalidKey("Supported SSH keys: Ed25519, RSA (3072+ bits), ECDSA")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error):
        raise InvalidKey("Invalid SSH public key encoding") from None
    canonical = parts[0] + " " + base64.b64encode(blob).decode()
    with tempfile.TemporaryDirectory(prefix="vgit-key-") as directory:
        path = Path(directory) / "key.pub"
        path.write_text(canonical + "\n", encoding="ascii")
        result = run(["-l", "-E", "sha256", "-f", str(path)], text=True)
    if result.returncode:
        raise InvalidKey("Invalid SSH public key")
    details = result.stdout.split()
    if len(details) < 2 or (parts[0] == "ssh-rsa" and int(details[0]) < 3072):
        raise InvalidKey("RSA keys must be at least 3072 bits")
    return canonical, details[1]


def verify(key, message, signature):
    with tempfile.TemporaryDirectory(prefix="vgit-verify-") as directory:
        directory = Path(directory)
        allowed = directory / "allowed_signers"
        allowed.write_text(f'vgit namespaces="{NAMESPACE}" {key}\n', encoding="ascii")
        signed = directory / "challenge.sig"
        signed.write_text(signature, encoding="utf-8")
        result = run(["-Y", "verify", "-f", str(allowed), "-I", "vgit", "-n", NAMESPACE, "-s", str(signed)], input=message.encode())
        return result.returncode == 0
