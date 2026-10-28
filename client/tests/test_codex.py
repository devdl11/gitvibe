import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from vgit import cli, codex, providers


class CodexTests(unittest.TestCase):
    def test_provider_passes_request_and_model_to_codex(self):
        env = {'GITVIBE_AI_PROVIDER': 'codex', 'GITVIBE_AI_MODEL': 'example-model', 'GITVIBE_CODEX_DEBUG': '1'}
        request = {'operation': 'digest', 'instructions': 'Return JSON', 'files': [{'content': 'private source'}]}

        def execute(command, **kwargs):
            self.assertIn('example-model', command)
            self.assertIn('read-only', command)
            self.assertIn('--ephemeral', command)
            self.assertIn('private source', kwargs['input'])
            Path(command[command.index('--output-last-message') + 1]).write_text(json.dumps({'knowledge': 'spec'}))
            return type('Result', (), {'returncode': 0})()

        stderr = io.StringIO()
        with patch('vgit.codex.subprocess.run', side_effect=execute), contextlib.redirect_stderr(stderr):
            self.assertEqual(providers.run(request, env), {'knowledge': 'spec'})
        self.assertIn('example-model', stderr.getvalue())
        self.assertNotIn('private source', stderr.getvalue())

    def test_dry_run_never_launches_and_warns_on_override(self):
        env = {'GITVIBE_AI_PROVIDER': 'codex', 'GITVIBE_AI_MODEL': 'example-model', 'GITVIBE_AI_COMMAND': 'other'}
        with patch.dict('os.environ', env, clear=True), patch('vgit.codex.subprocess.run') as run, contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main(['--codex-dry-run'])
        run.assert_not_called()
        self.assertIn('example-model', output.getvalue())
        self.assertIn('overrides', output.getvalue())

    def test_failures_and_message_fallback_do_not_produce_response(self):
        for model in ('', '-bad', 'bad\nmodel'):
            with self.subTest(model=model), self.assertRaises(ValueError):
                codex.command(model, '/tmp')
        with patch('vgit.codex.subprocess.run', return_value=type('Result', (), {'returncode': 1, 'stderr': 'Error: model unavailable'})()):
            with self.assertRaisesRegex(ValueError, '--codex-debug'):
                codex.run({'operation': 'digest'}, {'GITVIBE_AI_MODEL': 'model'})

    def test_codex_generates_messages(self):
        def execute(command, **kwargs):
            self.assertEqual(kwargs['timeout'], 30)
            Path(command[command.index('--output-last-message') + 1]).write_text('{"message":"Well done!"}')
            return type('Result', (), {'returncode': 0})()
        with patch('vgit.codex.subprocess.run', side_effect=execute):
            self.assertEqual(codex.run({'operation': 'message'}, {'GITVIBE_AI_MODEL': 'model'}), {'message': 'Well done!'})

    def test_debug_exposes_codex_error_only_when_requested(self):
        result = type('Result', (), {'returncode': 1, 'stderr': 'Error: model unavailable'})()
        for debug in (False, True):
            env = {'GITVIBE_AI_MODEL': 'model', 'GITVIBE_CODEX_DEBUG': '1' if debug else '0'}
            stderr = io.StringIO()
            with patch('vgit.codex.subprocess.run', return_value=result), contextlib.redirect_stderr(stderr):
                with self.assertRaises(ValueError):
                    codex.run({'operation': 'digest'}, env)
            self.assertEqual('Error: model unavailable' in stderr.getvalue(), debug)


if __name__ == '__main__':
    unittest.main()
