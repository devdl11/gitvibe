import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vgit import cli, stream


class StreamTests(unittest.TestCase):
    def test_remote_stream_stages_and_publishes(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'out'
            args = SimpleNamespace(server='http://localhost:7848', mode='server', repo='alice/app', revision=None, output=output, ai_command=None)
            events = [{'type': 'revision', 'id': '20261028T120000000000Z-a1b2c3d4'}, {'type': 'file', 'path': 'src/app.py', 'content': 'hello'}]
            with patch('vgit.stream.files', return_value=iter(events)), contextlib.redirect_stdout(io.StringIO()):
                cli.pull(args)
            self.assertEqual((output / 'src/app.py').read_text(), 'hello')

    def test_interrupted_and_unsafe_stream_do_not_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            args = SimpleNamespace(server='http://localhost', mode='server', repo='alice/app', revision=None, output=Path(temp) / 'out', ai_command=None)
            def broken():
                yield {'type': 'revision', 'id': '20261028T120000000000Z-a1b2c3d4'}
                yield {'type': 'file', 'path': 'ok.py', 'content': 'hello'}
                raise ValueError('interrupted')
            for events in (broken(), iter([{'type': 'revision', 'id': '20261028T120000000000Z-a1b2c3d4'}, {'type': 'file', 'path': '../bad', 'content': 'x'}])):
                with patch('vgit.stream.files', return_value=events), self.assertRaises(ValueError):
                    cli.pull(args)
                self.assertFalse(args.output.exists())
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_stream_requires_done(self):
        response = io.BytesIO((json.dumps({'type':'revision','id':'id'}) + '\n' + json.dumps({'type':'file','path':'x','content':'y'}) + '\n').encode())
        response.headers = SimpleNamespace(get_content_type=lambda: 'application/x-ndjson')
        with patch('vgit.stream.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                list(stream.files('http://localhost', 'repos/a/b/revisions'))
