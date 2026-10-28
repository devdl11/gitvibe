import contextlib
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from vgit import cli, intent


class IntentTests(unittest.TestCase):
    def test_selects_actions_and_preserves_arguments(self):
        cases = [
            ('auto', {}, ['auto']),
            ('init', {'repo': '/tmp/my repo'}, ['init', '/tmp/my repo']),
            ('commit', {'project': '/tmp/project'}, ['commit', '--project', '/tmp/project']),
            ('status', {}, ['status']),
            ('export', {'destination': '/tmp/out'}, ['export', '/tmp/out']),
            ('push', {'source': '.', 'repo': '/tmp/v'}, ['push', '.', '--repo', '/tmp/v']),
            ('push', {'repo': '../test2'}, ['push', '../test2']),
            ('pull', {'repo': '/tmp/v', 'output': 'out', 'revision': 'abc'}, ['pull', 'out', '--repo', '/tmp/v', '--revision', 'abc']),
            ('rebuild', {'repo': '/tmp/v', 'output': '/tmp/app'}, ['rebuild', '/tmp/app', '--repo', '/tmp/v']),
            ('log', {'repo': '/tmp/v'}, ['log', '--repo', '/tmp/v']),
            ('login', {'server': 'http://localhost', 'username': 'alice', 'ssh_key': 'key'}, ['login', '--server', 'http://localhost', '--username', 'alice', '--ssh-key', 'key']),
            ('ssh-key-list', {'server': 'http://localhost'}, ['ssh-key', 'list', '--server', 'http://localhost']),
            ('ssh-key-add', {'server': 'http://localhost', 'title': 'my laptop', 'public_key': 'id.pub'}, ['ssh-key', 'add', '--server', 'http://localhost', '--title', 'my laptop', '--public-key', 'id.pub']),
            ('ssh-key-remove', {'server': 'http://localhost', 'id': '1'}, ['ssh-key', 'remove', '--server', 'http://localhost', '1']),
        ]
        for action, arguments, expected in cases:
            with self.subTest(action=action):
                invoke = Mock(return_value={'action': action, 'arguments': arguments, 'reply': 'As you wish, my liege!'})
                selected, reply = intent.select('please do the thing', invoke, {})
                self.assertEqual(selected, expected)
                self.assertIn('liege', reply)
                self.assertEqual(invoke.call_args.args[1]['operation'], 'select_action')
                self.assertEqual(invoke.call_args.args[1]['prompt'], 'please do the thing')
                self.assertIn('medieval knight', invoke.call_args.args[1]['instructions'])

    def test_custom_persona_and_clarification(self):
        invoke = Mock(return_value={'action': None, 'arguments': {}, 'reply': 'Which repository?'})
        self.assertEqual(intent.select('save this', invoke, {'GITVIBE_PROMPT_SYSTEM_PROMPT': 'Speak like a pirate'}), (None, 'Which repository?'))
        self.assertIn('Speak like a pirate', invoke.call_args.args[1]['instructions'])
        self.assertIn('Valid actions', invoke.call_args.args[1]['instructions'])

    def test_rejects_invalid_model_output(self):
        invalid = [None, {}, {'action': 'shell', 'arguments': {}, 'reply': 'ok'},
                   {'action': 'push', 'arguments': {'source': 'x'}, 'reply': 'ok'},
                   {'action': 'init', 'arguments': {'repo': 'x', 'ai_command': 'evil'}, 'reply': 'ok'},
                   {'action': 'init', 'arguments': {'repo': '--help'}, 'reply': 'ok'},
                   {'action': None, 'arguments': {'repo': 'x'}, 'reply': 'ok'}]
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(ValueError):
                intent.select('do a thing', Mock(return_value=result), {})

    def test_both_interfaces_create_repository(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            first = Path(directory) / 'prompt repo'
            second = Path(directory) / 'command'
            with patch.object(cli, 'model', return_value={'action': 'init', 'arguments': {'repo': str(first)}, 'reply': 'At once, my liege!'}):
                cli.main(['I need a new knowledge repository'])
            cli.main(['--skill-issue', 'init', str(second)])
            self.assertTrue((first / '.vgit/KNOWLEDGE/revisions').is_dir())
            self.assertTrue((second / '.vgit/KNOWLEDGE/revisions').is_dir())

    def test_clarification_does_not_run_action(self):
        with patch.object(cli, 'model', return_value={'action': None, 'arguments': {}, 'reply': 'Which repo?'}), patch.object(cli, 'init') as init, contextlib.redirect_stderr(io.StringIO()) as err:
            cli.main(['make a repo'])
            self.assertIn('Which repo?', err.getvalue())
            init.assert_not_called()
