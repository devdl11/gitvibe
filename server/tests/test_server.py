import io
import json
import unittest
from unittest.mock import patch

from vgit_server.app import Handler


class ServerTests(unittest.TestCase):
    def test_health_and_unknown_route(self):
        for path, status, expected in (
            ("/health", 200, {"status": "ok", "service": "vgit-server", "version": "0.1.0"}),
            ("/missing", 404, {"error": "Endpoint not found"}),
        ):
            with self.subTest(path=path):
                class Connection:
                    def __init__(self):
                        self.output = bytearray()

                    def makefile(self, *args):
                        return io.BytesIO(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())

                    def sendall(self, data):
                        self.output.extend(data)

                connection = Connection()
                with patch.object(Handler, "log_message"):
                    Handler(connection, ("127.0.0.1", 12345), object())
                headers, body = bytes(connection.output).split(b"\r\n\r\n", 1)
                self.assertIn(f" {status} ".encode(), headers)
                self.assertIn(b"Content-Type: application/json", headers)
                self.assertIn(f"Content-Length: {len(body)}".encode(), headers)
                payload = json.loads(body)
                if status >= 400:
                    self.assertTrue(payload.pop("vibe"))
                self.assertEqual(payload, expected)
