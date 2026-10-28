import io
import contextlib
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from vgit import cli, providers


class ProviderTests(unittest.TestCase):
    def test_provider_requests_for_both_operations(self):
        for provider in ('openai', 'ollama', 'lmstudio'):
            for operation in ('digest', 'reconstruct'):
                with self.subTest(provider=provider, operation=operation):
                    expected = {'knowledge': 'spec'} if operation == 'digest' else {'files': [{'path': 'a.py', 'content': 'pass'}]}
                    message = {'content': json.dumps(expected)}
                    response = {'message': message} if provider == 'ollama' else {'choices': [{'message': message}]}
                    env = {'GITVIBE_AI_PROVIDER': provider, 'GITVIBE_AI_MODEL': 'test', 'OPENAI_API_KEY': 'secret'}
                    with patch('vgit.providers.build_opener') as opener:
                        opener.return_value.open.return_value.__enter__.return_value = io.BytesIO(json.dumps(response).encode())
                        self.assertEqual(providers.run({'operation': operation, 'instructions': 'Return JSON'}, env), expected)
                        request = opener.return_value.open.call_args.args[0]
                        payload = json.loads(request.data)
                        self.assertEqual(json.loads(payload['messages'][1]['content'])['operation'], operation)
                        if provider == 'lmstudio':
                            self.assertEqual(payload['response_format']['type'], 'json_schema')
                            self.assertEqual(payload['response_format']['json_schema']['schema']['type'], 'object')
                        if provider == 'openai':
                            self.assertEqual(request.get_header('Authorization'), 'Bearer secret')

    def test_invalid_configuration_and_response(self):
        for env in ({}, {'GITVIBE_AI_PROVIDER': 'ollama'}, {'GITVIBE_AI_PROVIDER': 'openai', 'GITVIBE_AI_MODEL': 'test'}, {'GITVIBE_AI_PROVIDER': 'ollama', 'GITVIBE_AI_MODEL': 'test', 'GITVIBE_AI_URL': 'http://remote.example'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                providers.run({}, env)
        with patch('vgit.providers.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = io.BytesIO(b'bad json')
            with self.assertRaises(ValueError):
                providers.run({}, {'GITVIBE_AI_PROVIDER': 'ollama', 'GITVIBE_AI_MODEL': 'test'})

    def test_adapter_precedence(self):
        with patch.dict('os.environ', {'GITVIBE_AI_COMMAND': 'custom', 'GITVIBE_AI_PROVIDER': 'ollama'}, clear=True), patch('vgit.cli.subprocess.run') as subprocess, patch('vgit.cli.providers.run') as builtin:
            subprocess.return_value.returncode = 0
            subprocess.return_value.stdout = '{}'
            cli.model('explicit', {})
            self.assertEqual(subprocess.call_args.args[0], ['explicit'])
            cli.model(None, {})
            self.assertEqual(subprocess.call_args.args[0], ['custom'])
            builtin.assert_not_called()
        with patch.dict('os.environ', {}, clear=True), patch('vgit.cli.providers.run', return_value={'knowledge': 'spec'}) as builtin:
            self.assertEqual(cli.model(None, {}), {'knowledge': 'spec'})
            builtin.assert_called_once()

    def test_provider_errors_are_actionable(self):
        env = {'GITVIBE_AI_PROVIDER': 'ollama', 'GITVIBE_AI_MODEL': 'qwen3-coder:30b'}
        for error, expected in [(HTTPError('http://localhost', 404, 'not found', {}, None), 'HTTP 404'),
                                (URLError('connection refused'), 'Cannot connect'),
                                (TimeoutError(), 'timed out')]:
            with self.subTest(error=error), patch('vgit.providers.build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(ValueError, expected):
                    providers.run({'instructions': 'Return JSON'}, env)

    def test_provider_debug_shows_http_error_body_only_when_enabled(self):
        for debug in (False, True):
            error = HTTPError('http://localhost', 400, 'bad request', {}, io.BytesIO(b'{"error":"context too long"}'))
            env = {'GITVIBE_AI_PROVIDER': 'lmstudio', 'GITVIBE_AI_MODEL': 'model', 'GITVIBE_PROVIDER_DEBUG': '1' if debug else '0'}
            with patch('vgit.providers.build_opener') as opener, contextlib.redirect_stderr(io.StringIO()) as output:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(ValueError, 'HTTP 400'):
                    providers.run({'instructions': 'Return JSON'}, env)
            self.assertEqual('context too long' in output.getvalue(), debug)
