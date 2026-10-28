import io
import json
import unittest
from unittest.mock import Mock, patch

from vgit_server import messages
from vgit_server.digest import DigestConfig, DigestError


class MessageTests(unittest.TestCase):
    def test_generation_and_fallback(self):
        config = Mock()
        config.message.return_value = 'Who goes there?'
        self.assertEqual(messages.render(config, 401), 'Who goes there?')
        self.assertEqual(config.message.call_args.args, (401, 'Authentication is required or failed.'))
        config.message.side_effect = DigestError('offline')
        self.assertIn('authenticate', messages.render(config, 401))

    def test_provider_request_contains_only_status_context(self):
        config = DigestConfig('openai', 'model', 'https://example.com', 'secret')
        result = {'choices': [{'message': {'content': json.dumps({'message': 'Who goes there?'})}}]}
        with patch('vgit_server.digest.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = io.BytesIO(json.dumps(result).encode())
            self.assertEqual(config.message(401, 'Authentication failed.'), 'Who goes there?')
            request = opener.return_value.open.call_args.args[0]
            payload = json.loads(request.data)
            self.assertNotIn('secret', json.dumps(payload))
            self.assertEqual(json.loads(payload['messages'][1]['content'])['status'], 401)
