import io
import json
import unittest
from unittest.mock import patch

from vgit_server.digest import DigestConfig, DigestError


class DigestTests(unittest.TestCase):
    def test_configuration(self):
        self.assertIsNone(DigestConfig.from_env({}))
        for provider in ('openai', 'ollama', 'lmstudio'):
            config = DigestConfig.from_env({'GITVIBE_DIGEST_PROVIDER': provider, 'GITVIBE_DIGEST_MODEL': 'test', 'OPENAI_API_KEY': 'secret'})
            self.assertEqual(config.provider, provider)
            self.assertNotIn('secret', repr(config))
        with self.assertRaises(DigestError):
            DigestConfig.from_env({'GITVIBE_DIGEST_PROVIDER': 'openai', 'GITVIBE_DIGEST_MODEL': 'test'})
        with self.assertRaises(DigestError):
            DigestConfig.from_env({'GITVIBE_DIGEST_PROVIDER': 'ollama', 'GITVIBE_DIGEST_MODEL': 'test', 'GITVIBE_DIGEST_URL': 'http://remote.example/api/chat'})

    def test_provider_requests_and_validation(self):
        for provider in ('ollama', 'openai', 'lmstudio'):
            with self.subTest(provider=provider):
                config = DigestConfig.from_env({'GITVIBE_DIGEST_PROVIDER': provider, 'GITVIBE_DIGEST_MODEL': 'test', 'OPENAI_API_KEY': 'secret'})
                message = {'content': json.dumps({'knowledge': 'Project specification'})}
                result = {'message': message} if provider == 'ollama' else {'choices': [{'message': message}]}
                with patch('vgit_server.digest.build_opener') as opener:
                    opener.return_value.open.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
                    self.assertEqual(config.digest([{'path': 'a.py', 'content': 'pass'}]), 'Project specification')
                    request = opener.return_value.open.call_args.args[0]
                    payload = json.loads(request.data)
                    self.assertEqual(payload['model'], 'test')
                    self.assertEqual(payload['messages'][1]['role'], 'user')
                    if provider == 'lmstudio':
                        self.assertEqual(payload['response_format']['type'], 'json_schema')
                    if provider == 'openai':
                        self.assertEqual(request.get_header('Authorization'), 'Bearer secret')
                    message['content'] = json.dumps({'knowledge': '```code```'})
                    opener.return_value.open.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
                    with self.assertRaises(DigestError):
                        config.digest([])
