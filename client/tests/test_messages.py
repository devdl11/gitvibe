import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from vgit import cli, messages


class MessageTests(unittest.TestCase):
    def test_generated_message_and_private_error_context(self):
        invoke = Mock(return_value={'message': 'Halt! Who goes there?'})
        with patch.dict('os.environ', {'GITVIBE_MESSAGE_SYSTEM_PROMPT': 'Speak like a pirate'}):
            result = messages.render(invoke, 'login', ValueError('Server returned HTTP 401: secret-token'))
        self.assertEqual(result, 'Halt! Who goes there?')
        request = invoke.call_args.args[1]
        self.assertEqual(request['outcome'], 'Authentication is required or failed.')
        self.assertNotIn('secret-token', str(request))
        self.assertIn('pirate', request['instructions'])

    def test_model_failure_and_invalid_message_fall_back(self):
        for invoke in (Mock(side_effect=ValueError('offline')), Mock(return_value={'message': '\x1b[31m'})):
            self.assertIn('failed', messages.render(invoke, 'push', ValueError('failed')))

    def test_cli_preserves_original_error_and_exit_code(self):
        with patch.object(cli, 'model', return_value={'message': 'Alas, the gate is shut!'}), contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                cli.main(['--skill-issue', 'log', '--repo', '/nonexistent-vgit-test'])
        self.assertEqual(error.exception.code, 1)
        self.assertIn('Alas, the gate is shut!', output.getvalue())
        self.assertIn('Not a vgit repository', output.getvalue())
