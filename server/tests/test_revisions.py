import tempfile
from pathlib import Path
import unittest

from vgit_server.store import APIError, Store


class RevisionTests(unittest.TestCase):
    def test_access_history_and_cascade(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'accounts.sqlite3')
            for user in ('alice', 'bob'):
                store.dispatch('POST', ['accounts'], {'username': user, 'password': 'a long password here'})
            token = store.dispatch('POST', ['auth', 'login'], {'username': 'alice', 'password': 'a long password here'})[1]['token']
            bob = store.dispatch('POST', ['auth', 'login'], {'username': 'bob', 'password': 'a long password here'})[1]['token']
            store.dispatch('POST', ['repos'], {'name': 'project'}, token)
            route = ['repos', 'alice', 'project', 'revisions']
            with self.assertRaises(APIError) as error:
                store.dispatch('GET', route, {}, bob)
            self.assertEqual(error.exception.status, 404)
            first = store.dispatch('POST', route, {'knowledge': 'First revision'}, token)[1]
            second = store.dispatch('POST', route, {'knowledge': 'Second revision'}, token)[1]
            self.assertEqual(second['parent'], first['id'])
            self.assertEqual(store.dispatch('GET', route, {}, token)[1]['knowledge'], 'Second revision')
            self.assertEqual(store.dispatch('GET', route + [first['id']], {}, token)[1]['knowledge'], 'First revision')
            store.dispatch('PUT', ['repos', 'alice', 'project', 'collaborators', 'bob'], {'role': 'read'}, token)
            self.assertEqual(store.dispatch('GET', route, {}, bob)[0], 200)
            with self.assertRaises(APIError):
                store.dispatch('POST', route, {'knowledge': 'no'}, bob)
            store.dispatch('DELETE', ['repos', 'alice', 'project'], {'confirm': 'alice/project'}, token)
            with store.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM revisions').fetchone()[0], 0)
