import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = [patch.object(app, 'DB_PATH', Path(self.temp.name) / 'jobs.sqlite3'),
                        patch.object(app, 'MCP_TOKEN_FILE', Path(self.temp.name) / 'mcp.token'),
                        patch.object(app, 'BRIDGE_WAIT_SECONDS', 0.5),
                        patch.object(app, 'TRANSPORT', 'extension')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        app.BRIDGE_STATES.clear()
        app.BRIDGE_CURRENT = None
        app.init_db()
        self.a, self.b = 'a' * 32, 'b' * 32

    def identity(self, account):
        return {'protocol_version': 2, 'account_id': account}

    def ping(self, account, ready=True, name=None):
        return app.bridge_ping({**self.identity(account), 'account_name': name or f'Test {account[0]}',
                                'ready': ready, 'url': 'https://affiliate.shopee.co.id/offer/custom_link'})

    def enqueue(self, account, **kwargs):
        return app.enqueue_job({'account_id': account, 'links': ['https://shopee.co.id/test-product'],
                                'tags': ['BridgeTest']}, **kwargs)[0]

    def start_job(self, account):
        self.ping(account)
        job = self.enqueue(account)
        worker = app.Worker()
        row = worker.claim()
        thread = threading.Thread(target=worker.process, args=(row,))
        thread.start()
        self.addCleanup(thread.join, 2)
        deadline = time.monotonic() + 1
        while app.BRIDGE_CURRENT is None and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertIsNotNone(app.BRIDGE_CURRENT)
        return job, thread

    def result_body(self, account, delivery):
        return {**self.identity(account), 'job_id': delivery['job']['id'],
                'lease_token': delivery['lease_token'],
                'results': [{'shortLink': 'https://s.shopee.co.id/BridgeTest1', 'failCode': 0}]}

    def test_ten_accounts_route_only_the_selected_profile(self):
        accounts = [f'{i:032x}' for i in range(10)]
        for account in accounts:
            self.ping(account)
        job, thread = self.start_job(accounts[7])
        for account in accounts:
            if account != accounts[7]:
                self.assertIsNone(app.bridge_next(self.identity(account))['job'])
        delivery = app.bridge_next(self.identity(accounts[7]))
        self.assertEqual(delivery['job']['id'], job['id'])
        app.bridge_result(self.result_body(accounts[7], delivery))
        thread.join(1)
        stored = app.get_job(job['id'])
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(stored['account_id'], accounts[7])
        self.assertTrue(stored['result'][0]['shortLink'])
        self.assertEqual(len(app.list_affiliate_accounts()), 10)

    def test_delivery_is_once_and_foreign_or_duplicate_results_are_rejected(self):
        self.ping(self.b)
        job, thread = self.start_job(self.a)
        delivery = app.bridge_next(self.identity(self.a))
        self.assertIsNone(app.bridge_next(self.identity(self.a))['job'])
        with self.assertRaises(ValueError):
            app.bridge_result(self.result_body(self.b, delivery))
        wrong = self.result_body(self.a, delivery)
        wrong['lease_token'] = 'invalid'
        with self.assertRaises(ValueError):
            app.bridge_result(wrong)
        result = self.result_body(self.a, delivery)
        app.bridge_result(result)
        with self.assertRaises(ValueError):
            app.bridge_result(result)
        thread.join(1)
        self.assertEqual(app.get_job(job['id'])['status'], 'completed')

    def test_offline_account_does_not_block_ready_account(self):
        self.ping(self.a, False)
        offline = self.enqueue(self.a)
        self.ping(self.b)
        ready = self.enqueue(self.b)
        self.assertEqual(app.Worker().claim()['id'], ready['id'])
        self.assertEqual(app.get_job(offline['id'])['status'], 'queued')
        self.assertFalse(app.list_affiliate_accounts()[0]['ready'])

    def test_heartbeat_expires_independently(self):
        self.ping(self.a)
        self.ping(self.b)
        app.BRIDGE_STATES[self.a]['seen'] -= 11
        self.assertEqual(app.ready_account_ids(), [self.b])
        self.ping(self.b, False)
        self.assertFalse(app.bridge_is_connected())

    def test_missing_unknown_or_legacy_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            app.bridge_ping({'ready': True})
        with self.assertRaises(ValueError):
            app.bridge_next({})
        with self.assertRaises(ValueError):
            app.enqueue_job({'links': ['https://shopee.co.id/test']})
        with self.assertRaises(ValueError):
            self.enqueue(self.a)

    def test_only_custom_link_path_is_ready(self):
        self.ping(self.a)
        app.bridge_ping({**self.identity(self.a), 'account_name': 'Test', 'ready': True,
                         'url': 'https://affiliate.shopee.co.id/offer/product_offer'})
        self.assertFalse(app.bridge_is_connected())

    def test_idempotency_cannot_reuse_another_account_or_payload(self):
        self.ping(self.a)
        self.ping(self.b)
        first = self.enqueue(self.a, idempotency_key='unique-test')
        self.assertEqual(self.enqueue(self.a, idempotency_key='unique-test')['id'], first['id'])
        with self.assertRaises(ValueError):
            self.enqueue(self.b, idempotency_key='unique-test')
        with self.assertRaises(ValueError):
            app.enqueue_job({'account_id': self.a, 'links': ['https://shopee.co.id/other']},
                            idempotency_key='unique-test')

    def test_timeout_needs_session_and_late_results_cannot_complete(self):
        job, thread = self.start_job(self.a)
        delivery = app.bridge_next(self.identity(self.a))
        thread.join(1)
        self.assertEqual(app.get_job(job['id'])['status'], 'needs_session')
        with self.assertRaises(ValueError):
            app.bridge_result(self.result_body(self.a, delivery))

    def test_no_short_link_cannot_be_completed(self):
        job, thread = self.start_job(self.a)
        delivery = app.bridge_next(self.identity(self.a))
        result = self.result_body(self.a, delivery)
        result['results'] = [{'failCode': 0}]
        app.bridge_result(result)
        thread.join(1)
        self.assertEqual(app.get_job(job['id'])['status'], 'failed')

    def test_restart_preserves_target_and_recovers_running(self):
        self.ping(self.a)
        job = self.enqueue(self.a)
        app.Worker().claim()
        app.init_db()
        app.BRIDGE_STATES.clear()
        self.assertEqual(app.get_job(job['id'])['status'], 'queued')
        self.assertEqual(app.get_job(job['id'])['account_id'], self.a)
        self.assertIsNone(app.Worker().claim())
        self.ping(self.a)
        self.assertEqual(app.Worker().claim()['id'], job['id'])

    def test_old_database_migration_preserves_history_and_stops_unassigned_jobs(self):
        with app.db() as conn:
            conn.execute('DROP TABLE jobs')
            conn.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,created_at TEXT,updated_at TEXT,status TEXT,'
                         'links_json TEXT,tags_json TEXT,result_json TEXT,error TEXT,attempt INTEGER DEFAULT 0)')
            for status in ('queued', 'running', 'completed'):
                conn.execute('INSERT INTO jobs(id,status,links_json,tags_json) VALUES(?,?,?,?)',
                             (status, status, '[]', '[]'))
        app.init_db()
        self.assertEqual(app.get_job('queued')['status'], 'needs_session')
        self.assertEqual(app.get_job('running')['status'], 'needs_session')
        self.assertEqual(app.get_job('completed')['status'], 'completed')
        self.assertIsNone(app.get_job('completed')['account_id'])
        app.init_db()
        with app.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 3)

    def test_account_rename_does_not_relabel_history(self):
        self.ping(self.a, name='Original')
        job = self.enqueue(self.a)
        self.ping(self.a, name='Renamed')
        self.assertEqual(app.get_job(job['id'])['account_name'], 'Original')
        self.assertEqual(app.list_affiliate_accounts()[0]['name'], 'Renamed')

    def test_mcp_can_list_accounts_and_target_one(self):
        self.ping(self.a)
        listing, _ = app.mcp_call_tool('shopee_list_accounts', {})
        self.assertEqual(listing['accounts'][0]['id'], self.a)
        result, job_id = app.mcp_call_tool('shopee_generate_short_links',
                                         {'account_id': self.a, 'links': ['https://shopee.co.id/test']})
        self.assertEqual(result['job']['account_id'], self.a)
        self.assertEqual(result['job_id'], job_id)

    def test_http_requires_auth_and_persists_target(self):
        self.ping(self.a)
        with app.db() as conn:
            conn.execute("INSERT INTO users(email,password_hash,must_change_password) VALUES('test@example.invalid','unused',0)")
            user_id = conn.execute('SELECT id FROM users').fetchone()[0]
        session = app.create_session(user_id)
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None, authenticated=True):
            client = http.client.HTTPConnection(*server.server_address, timeout=2)
            headers = {'Content-Type': 'application/json'}
            if authenticated:
                headers['Cookie'] = f'{app.AUTH_COOKIE_NAME}={session}'
            client.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = client.getresponse()
            status, data = response.status, json.loads(response.read())
            client.close()
            return status, data
        for path in ('/api/accounts', '/api/jobs', '/api/bridge/next'):
            self.assertEqual(request('GET', path, authenticated=False)[0], 401)
        self.assertEqual(request('POST', '/api/bridge/ping', {}, False)[0], 401)
        self.assertEqual(request('GET', '/api/accounts')[1]['accounts'][0]['id'], self.a)
        status, accepted = request('POST', '/api/jobs', {'account_id': self.a, 'links': ['https://shopee.co.id/test']})
        self.assertEqual(status, 202)
        status, stored = request('GET', f"/api/jobs/{accepted['id']}")
        self.assertEqual(stored['account_id'], self.a)
        self.assertEqual(request('GET', '/api/bridge/next')[0], 400)
        self.assertEqual(request('POST', '/api/bridge/next', self.identity(self.b))[1], {'job': None})
        self.assertNotIn('lease_token', json.dumps(request('GET', '/api/health')[1]))


if __name__ == '__main__':
    unittest.main()
