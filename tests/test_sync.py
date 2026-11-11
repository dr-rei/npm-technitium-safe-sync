import pathlib
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import sync


def cfg(path, dry_run=False, deletions=False):
    return dict(npm_url='http://npm:81', npm_identity='reader@example.com', npm_secret='x',
                dns_url='http://dns:5380', dns_token='x', ip='192.0.2.20',
                zones=('apps.example.com',), interval=300, ttl=120,
                dry_run=dry_run, deletions=deletions, delete_grace=86400,
                max_deletes=5, allow_empty=False, state_path=path)


class Tests(unittest.TestCase):
    def setUp(self):
        # These unit cases mock the mutation boundary; separate tests exercise re-reads.
        guard = patch.object(sync, 'check_unchanged')
        guard.start()
        self.addCleanup(guard.stop)

    def test_names(self):
        self.assertEqual(sync.normalized_name(' Foo.APPs.example.com. '), 'foo.apps.example.com')
        self.assertIsNone(sync.normalized_name('*.apps.example.com'))
        self.assertIsNone(sync.normalized_name('bad_name.apps.example.com'))
        self.assertIsNone(sync.normalized_name('a'))

    def test_npm_auth_and_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            conf = cfg(pathlib.Path(d)/'state.json')
            def fakeapi(method, url, params=None, payload=None, headers=None):
                if url.endswith('/api/tokens'):
                    self.assertEqual(method, 'POST')
                    self.assertEqual(payload['identity'], conf['npm_identity'])
                    return {'token': 'read-token'}
                self.assertEqual(headers['Authorization'], 'Bearer read-token')
                return [
                    {'enabled': True, 'domain_names': ['ERP.apps.example.com', '*.apps.example.com', 'other.example.com']},
                    {'enabled': False, 'domain_names': ['disabled.apps.example.com']},
                ]
            with patch.object(sync, 'api', fakeapi):
                self.assertEqual(sync.npm_hosts(conf), {'erp.apps.example.com'})

    def test_dry_run_no_write_or_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)/'state.json'
            conf = cfg(path, dry_run=True)
            with patch.object(sync, 'tech_call') as tech:
                result = sync.reconcile(conf, {'erp.apps.example.com'}, {}, sync.state_load(path), 10)
            self.assertEqual(result['added'], 1)
            self.assertFalse(path.exists())
            tech.assert_not_called()

    def test_create_then_update_owned(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)/'state.json'
            conf = cfg(path)
            calls = []
            def call(conf, endpoint, params=None, write=False):
                calls.append((endpoint, params))
                return {'status': 'ok'}
            with patch.object(sync, 'tech_call', call):
                r = sync.reconcile(conf, {'erp.apps.example.com'}, {}, sync.state_load(path))
                self.assertEqual(r['added'], 1)
                self.assertEqual(sync.state_load(path)['owned']['erp.apps.example.com'], '192.0.2.20')
                conf['ip'] = '192.0.2.21'
                snapshot = {'erp.apps.example.com': [{'type': 'A', 'comments': sync.MARKER, 'disabled': False, 'rData': {'ipAddress': '192.0.2.20'}}]}
                r = sync.reconcile(conf, {'erp.apps.example.com'}, snapshot, sync.state_load(path))
                self.assertEqual(r['updated'], 1)
            self.assertTrue(calls[0][0].endswith('/add'))
            self.assertTrue(calls[1][0].endswith('/update'))

    def test_preserves_existing_unmanaged(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)/'state.json'
            conf = cfg(path)
            snapshot = {'erp.apps.example.com': [{'type': 'CNAME', 'rData': {'cname': 'another.example.com'}}]}
            with patch.object(sync, 'tech_call') as tech:
                r = sync.reconcile(conf, {'erp.apps.example.com'}, snapshot, sync.state_load(path))
            self.assertEqual(r['conflicts'], 1)
            tech.assert_not_called()

    def test_delete_requires_grace_and_owned_ip(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)/'state.json'
            conf = cfg(path, deletions=True)
            s = {'version': 1, 'owned': {'erp.apps.example.com': '192.0.2.20'}, 'stale_since': {}}
            dns = {'erp.apps.example.com': [{'type': 'A', 'comments': sync.MARKER, 'disabled': False, 'rData': {'ipAddress': '192.0.2.20'}}]}
            with patch.object(sync, 'tech_call') as tech:
                r = sync.reconcile(conf, {'other.apps.example.com'}, dns, s, now=100)
                self.assertEqual(r['pending'], 1)
                self.assertEqual(r['deleted'], 0)
                r = sync.reconcile(conf, {'other.apps.example.com'}, dns, s, now=86600)
                self.assertEqual(r['deleted'], 1)
                self.assertNotIn('erp.apps.example.com', sync.state_load(path)['owned'])
            self.assertTrue(any(x.args[1].endswith('/delete') for x in tech.mock_calls))

    def test_no_delete_when_dns_manually_changed(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d)/'state.json'
            conf = cfg(path, deletions=True)
            s = {'version': 1, 'owned': {'erp.apps.example.com': '192.0.2.20'}, 'stale_since': {'erp.apps.example.com': 1}}
            dns = {'erp.apps.example.com': [{'type': 'A', 'comments': sync.MARKER, 'disabled': False, 'rData': {'ipAddress': '192.0.2.99'}}]}
            with patch.object(sync, 'tech_call') as tech:
                r = sync.reconcile(conf, set(), dns, s, now=90000)
            self.assertEqual(r['deleted'], 0)
            self.assertEqual(r['conflicts'], 1)
            tech.assert_not_called()


if __name__ == '__main__':
    unittest.main()
