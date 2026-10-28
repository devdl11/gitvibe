import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vgit import cli


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "app.py").write_text('print("hello")\n')
        with contextlib.redirect_stdout(io.StringIO()):
            cli.init(SimpleNamespace(repo=self.repo, bare=True))
        self.args = SimpleNamespace(repo=self.repo, source=self.source, ai_command=None,
                                    output=self.base / "checkout", revision=None)

    def push(self):
        with patch.object(cli, "model", return_value={"knowledge": "A Python program that greets the world."}), contextlib.redirect_stdout(io.StringIO()):
            cli.push(self.args)

    def test_round_trip_keeps_only_markdown_and_preserves_source(self):
        self.push()
        files = [p for p in self.repo.rglob("*") if p.is_file()]
        self.assertTrue(all(p.suffix == ".md" for p in files))
        self.assertNotIn('print("hello")', "".join(p.read_text() for p in files))
        with patch.object(cli, "model", return_value={"files": [{"path": "src/main.py", "content": 'print("Hello, world!")\n'}]}), contextlib.redirect_stdout(io.StringIO()):
            cli.pull(self.args)
        self.assertTrue((self.args.output / "src/main.py").is_file())
        self.assertEqual((self.source / "app.py").read_text(), 'print("hello")\n')
        with self.assertRaises(ValueError):
            cli.pull(self.args)

    def test_rejects_unsafe_paths_without_partial_output(self):
        self.push()
        for path in ("../escape.py", "/tmp/escape.py", ".git/config", "foo/../../escape", "C:/escape", "foo\\bar", "a//b"):
            with self.subTest(path=path), patch.object(cli, "model", return_value={"files": [{"path": path, "content": "oops"}]}):
                with self.assertRaises(ValueError):
                    cli.pull(self.args)
                self.assertFalse(self.args.output.exists())

    def test_conflicting_paths_and_failed_digest(self):
        self.push()
        for paths in (("a", "a/b"), ("a/b", "a"), ("a", "a")):
            with patch.object(cli, "model", return_value={"files": [{"path": p, "content": "x"} for p in paths]}):
                with self.assertRaises(ValueError):
                    cli.pull(self.args)
        with patch.object(cli, "model", return_value={"knowledge": "```python\nsecret\n```"}):
            with self.assertRaises(ValueError):
                cli.push(self.args)
        self.assertEqual(len(cli.revisions(self.repo)), 1)
        self.assertFalse((self.repo / "KNOWLEDGE/LOCK.md").exists())

    def test_ignores_hidden_files_dependencies_and_symlinks(self):
        (self.source / ".env.py").write_text("secret")
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules/a.js").write_text("dependency")
        (self.source / "link.py").symlink_to(self.source / "app.py")
        self.assertEqual([f["path"] for f in cli.collect(self.source, self.repo)], ["app.py"])

    def test_parent_and_previous_knowledge(self):
        self.push()
        parent = cli.revisions(self.repo)[0]
        with patch.object(cli, "model", return_value={"knowledge": "Updated program."}) as model, contextlib.redirect_stdout(io.StringIO()):
            cli.push(self.args)
        self.assertIn("greets the world", model.call_args.args[1]["previous_knowledge"])
        latest = cli.revisions(self.repo)[-1]
        self.assertIn(f"Parent: {parent}", cli.read_revision(self.repo, latest))

    def test_rebuild_replaces_existing_directory(self):
        self.push()
        (self.source / '.hidden').write_text('old')
        self.args.output = self.source
        with patch.object(cli, 'model', return_value={'files': [{'path': 'new/main.py', 'content': 'new'}]}), contextlib.redirect_stdout(io.StringIO()):
            cli.rebuild(self.args)
        self.assertEqual((self.source / 'new/main.py').read_text(), 'new')
        self.assertFalse((self.source / 'app.py').exists())
        self.assertFalse((self.source / '.hidden').exists())
        self.assertEqual(len(cli.revisions(self.repo)), 1)

    def test_rebuild_failure_preserves_original(self):
        self.push()
        self.args.output = self.source
        with patch.object(cli, 'model', return_value={'files': [{'path': '../bad', 'content': 'bad'}]}):
            with self.assertRaises(ValueError):
                cli.rebuild(self.args)
        self.assertEqual((self.source / 'app.py').read_text(), 'print("hello")\n')
        real_rename = cli.os.rename
        calls = []
        def fail_install(src, dst):
            calls.append((src, dst))
            if len(calls) == 2:
                raise OSError('install failed')
            return real_rename(src, dst)
        with patch.object(cli, 'model', return_value={'files': [{'path': 'new.py', 'content': 'new'}]}), patch.object(cli.os, 'rename', side_effect=fail_install):
            with self.assertRaises(OSError):
                cli.rebuild(self.args)
        self.assertEqual((self.source / 'app.py').read_text(), 'print("hello")\n')
        self.assertFalse((self.source / 'new.py').exists())

    def test_rebuild_rejects_repository_overlap_and_symlink(self):
        self.push()
        link = self.base / 'link'
        link.symlink_to(self.source, target_is_directory=True)
        for target in (self.repo, self.base, self.repo / 'KNOWLEDGE', link, self.base / 'missing'):
            with self.subTest(target=target), patch.object(cli, 'model') as model:
                self.args.output = target
                with self.assertRaises(ValueError):
                    cli.rebuild(self.args)
                model.assert_not_called()

    def test_remote_pull_reconstructs_fetched_knowledge(self):
        self.args.repo = 'alice/project'
        self.args.server = 'http://localhost:7848'
        revision = '20261028T120000000000Z-a1b2c3d4'
        with patch.object(cli, 'api', return_value={'id': revision, 'knowledge': 'A project'}) as api, patch.object(cli, 'model', return_value={'files': [{'path': 'app.py', 'content': 'new'}]}) as model, contextlib.redirect_stdout(io.StringIO()):
            cli.pull(self.args)
        self.assertEqual((self.args.output / 'app.py').read_text(), 'new')
        self.assertEqual(api.call_args.args[2], 'repos/alice/project/revisions')
        self.assertEqual(model.call_args.args[1]['knowledge'], 'A project')

    def test_command_protocol(self):
        import sys
        adapter = self.base / "adapter.py"
        adapter.write_text('import json, sys\nr=json.load(sys.stdin)\nprint(json.dumps({"knowledge": r["operation"]}))\n')
        result = cli.model(f'{sys.executable} {adapter}', {"operation": "digest"})
        self.assertEqual(result, {"knowledge": "digest"})

    def test_local_commit_status_and_incremental_export(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.init(SimpleNamespace(repo=self.source))
        local = self.source / '.vgit'
        args = SimpleNamespace(project=self.source, ai_command=None)
        with patch.object(cli, 'model', return_value={'knowledge': 'First spec'}) as model, contextlib.redirect_stdout(io.StringIO()):
            cli.commit(args)
        self.assertEqual([f['path'] for f in model.call_args.args[1]['files']], ['app.py'])
        (self.source / 'app.py').write_text('changed')
        (self.source / 'new.py').write_text('new')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.status(args)
        self.assertIn('M app.py', out.getvalue())
        self.assertIn('A new.py', out.getvalue())
        destination = self.base / 'export'
        export_args = SimpleNamespace(project=self.source, destination=destination)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.export(export_args)
            with patch.object(cli, 'model', return_value={'knowledge': 'Second spec'}):
                cli.commit(args)
            cli.export(export_args)
        self.assertEqual(cli.revisions(local), cli.revisions(destination))
        self.assertEqual(len(cli.revisions(destination)), 2)
        (self.source / 'app.py').unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.status(args)
        self.assertIn('D app.py', out.getvalue())
        with self.assertRaises(ValueError):
            cli.export(SimpleNamespace(project=self.source, destination=self.source / 'nested'))

    def test_export_rejects_conflicting_history(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.init(SimpleNamespace(repo=self.source))
            with patch.object(cli, 'model', return_value={'knowledge': 'spec'}):
                cli.commit(SimpleNamespace(project=self.source, ai_command=None))
            cli.export(SimpleNamespace(project=self.source, destination=self.base / 'out'))
        dest = self.base / 'out'
        revision = cli.revisions(dest)[0]
        (dest / 'KNOWLEDGE' / 'revisions' / (revision + '.md')).write_text('different')
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            cli.export(SimpleNamespace(project=self.source, destination=dest))

    def test_local_commands_without_model_and_empty_status(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(['--skill-issue', 'init', str(self.source)])
            with patch.object(cli, 'model', return_value={'knowledge': 'spec'}):
                cli.commit(SimpleNamespace(project=self.source, ai_command=None))
            (self.source / 'app.py').unlink()
            with patch.object(cli, 'model') as model, contextlib.redirect_stdout(io.StringIO()) as out:
                cli.main(['--skill-issue', 'status', '--project', str(self.source)])
                cli.main(['--skill-issue', 'export', str(self.base / 'out'), '--project', str(self.source)])
                self.assertEqual(model.call_args.args[1]['operation'], 'message')
            self.assertIn('D app.py', out.getvalue())

    def test_push_path_exports_local_commits_without_model(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.init(SimpleNamespace(repo=self.source))
            with patch.object(cli, 'model', return_value={'knowledge': 'spec'}):
                cli.commit(SimpleNamespace(project=self.source, ai_command=None))
            with patch('vgit.cli.local_project', return_value=(self.source, cli.repository(self.source / '.vgit'))), patch.object(cli, 'model') as model:
                cli.main(['--skill-issue', 'push', str(self.base / 'out')])
                self.assertEqual(model.call_args.args[1]['operation'], 'message')
            self.assertEqual(cli.revisions(self.source / '.vgit'), cli.revisions(self.base / 'out'))

    def test_auto_initializes_commits_and_reports_clean(self):
        with patch('vgit.cli.Path.cwd', return_value=self.source), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            with patch.object(cli, 'model') as model:
                cli.main(['--skill-issue', 'auto'])
                self.assertEqual(model.call_args.args[1]['operation'], 'message')
            self.assertTrue((self.source / '.vgit/KNOWLEDGE/revisions').is_dir())
            with patch.object(cli, 'model', return_value={'knowledge': 'spec'}) as model:
                cli.main(['--skill-issue', 'auto'])
                self.assertEqual([call.args[1]['operation'] for call in model.call_args_list], ['digest', 'message'])
            with patch.object(cli, 'model') as model:
                cli.main(['--skill-issue', 'auto'])
                self.assertEqual(model.call_args.args[1]['operation'], 'message')
            self.assertIn('nothing to commit', out.getvalue())
            (self.source / 'app.py').write_text('changed')
            with patch.object(cli, 'model', return_value={'knowledge': 'updated'}) as model:
                cli.main(['--skill-issue', 'auto'])
                self.assertEqual([call.args[1]['operation'] for call in model.call_args_list], ['digest', 'message'])


if __name__ == "__main__":
    unittest.main()
