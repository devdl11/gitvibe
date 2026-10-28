import io
import json
import unittest
from unittest.mock import patch

from vgit_server.digest import DigestConfig, DigestError


class ProviderStreamTests(unittest.TestCase):
    def test_provider_streams_files_before_completion(self):
        for provider in ('ollama', 'openai', 'lmstudio'):
            with self.subTest(provider=provider):
                config = DigestConfig(provider, 'model', 'http://localhost', '')
                records = ['{"path":"a.py","content":"x"}\n', '{"path":"b.py","content":"y"}\n']
                if provider == 'ollama':
                    lines = [json.dumps({'message': {'content': x}, 'done': False}) for x in records]
                    lines.append(json.dumps({'message': {'content': ''}, 'done': True}))
                else:
                    lines = ['data: ' + json.dumps({'choices': [{'delta': {'content': x}, 'finish_reason': None}]}) for x in records]
                    lines += ['data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}), 'data: [DONE]']
                source = io.BytesIO(('\n'.join(lines) + '\n').encode())
                with patch('vgit_server.digest.build_opener') as opener:
                    opener.return_value.open.return_value.__enter__.return_value = source
                    gen = config.stream_files('spec')
                    self.assertEqual(next(gen), {'path': 'a.py', 'content': 'x'})
                    self.assertLess(source.tell(), len(source.getvalue()))
                    self.assertEqual(list(gen), [{'path': 'b.py', 'content': 'y'}])

    def test_incomplete_stream_fails(self):
        config = DigestConfig('ollama', 'model', 'http://localhost')
        with patch('vgit_server.digest.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = io.BytesIO(b'{"message":{"content":""},"done":false}\n')
            with self.assertRaises(DigestError):
                list(config.stream_files('spec'))

class StreamHTTPTests(unittest.TestCase):
    def test_authorization_precedes_model_and_streams_events(self):
        from types import SimpleNamespace
        from vgit_server.app import Handler
        from vgit_server.store import APIError
        class Connection:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, *args):
                return io.BytesIO(b'GET /api/repos/alice/app/revisions/stream HTTP/1.0\r\nHost: localhost\r\n\r\n')
            def sendall(self, data):
                self.output.extend(data)
        class Config:
            def stream_files(self, knowledge):
                self.knowledge = knowledge
                yield {'path': 'app.py', 'content': 'hello'}
        class Store:
            def dispatch(self, method, parts, data, token):
                self.parts = parts
                if self.denied:
                    raise APIError(404, 'Repository not found')
                return 200, {'id': 'revision', 'knowledge': 'spec'}
        store, config = Store(), Config()
        server = SimpleNamespace(store=store, digest_config=config)
        store.denied = True
        conn = Connection()
        with patch.object(Handler, 'log_message'):
            Handler(conn, ('127.0.0.1', 1), server)
        self.assertIn(b' 404 ', conn.output)
        self.assertFalse(hasattr(config, 'knowledge'))
        store.denied = False
        conn = Connection()
        with patch.object(Handler, 'log_message'):
            Handler(conn, ('127.0.0.1', 1), server)
        headers, body = bytes(conn.output).split(b'\r\n\r\n', 1)
        self.assertIn(b'application/x-ndjson', headers)
        events = [json.loads(line) for line in body.splitlines()]
        self.assertEqual([e['type'] for e in events], ['revision', 'file', 'done'])
        self.assertEqual(store.parts, ['repos', 'alice', 'app', 'revisions'])
        self.assertEqual(config.knowledge, 'spec')
