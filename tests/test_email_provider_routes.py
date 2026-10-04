import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard
from test_marketing_workspace import FakeConnection


class EmailProviderRoutes(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()
        self.url = '/api/brands/27/email-provider'
        self.brand = {'id': 27, 'project_id': 10, 'classification': 'core', 'lifecycle': 'active'}
        self.config = {'provider': 'brevo', 'credential_ref': '/home/agency/.config/agency/deployden-email.env', 'credential_name': 'BREVO_API_KEY', 'sender_email': 'hello@deployden.tech'}

    def module(self):
        import marketing_email_provider
        return marketing_email_provider

    def post(self, payload):
        return self.client.post(self.url, json=payload, headers={'Origin': 'http://localhost'})

    def test_missing_origin_rejected(self):
        with mock.patch.object(dashboard.models, 'db') as db:
            response = self.client.post(self.url, json={})
        self.assertEqual(response.status_code, 403)
        db.assert_not_called()

    def test_saved_reference_is_tracked_without_secret_read_or_send(self):
        conn = FakeConnection([self.brand, None, {'id': 50}])
        with mock.patch.object(dashboard.models, 'db', return_value=conn), mock.patch.object(self.module(), 'read_credential') as read:
            response = self.post({'action': 'save', 'digest': self.module().config_digest({}), 'config': self.config})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json['sending_enabled'])
        read.assert_not_called()
        self.assertEqual(conn.commits, 1)
        self.assertTrue(any('email_provider_setup' in sql for sql, _ in conn.cursor_value.calls))

    def test_cross_engagement_reference_cannot_be_saved(self):
        brand = {**self.brand, 'classification': 'engagement', 'local_path': '/home/agency/engagements/north'}
        conn = FakeConnection([brand, None])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.post({'action': 'save', 'digest': self.module().config_digest({}), 'config': {**self.config, 'credential_ref': '/home/agency/engagements/south/private/email.env'}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(conn.commits, 0)

    def test_api_key_field_is_never_saved(self):
        conn = FakeConnection([self.brand, None])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.post({'action': 'save', 'digest': self.module().config_digest({}), 'config': {**self.config, 'api_key': 'forbidden'}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(conn.commits, 0)

    def test_changed_configuration_hides_stale_evidence(self):
        conn = FakeConnection([self.brand, {'value': json.dumps(self.config)}, {'value': json.dumps({'config_digest': 'old', 'status': 'verified', 'authenticated': True})}])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['verification'], {'status': 'not_verified'})

    def test_exact_saved_digest_queues_read_only_verification(self):
        conn = FakeConnection([self.brand, {'value': json.dumps(self.config)}, None, {'id': 51}])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.post({'action': 'verify', 'digest': self.module().config_digest(self.config)})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json['task_id'], 51)
        self.assertFalse(response.json['sending_enabled'])
        self.assertTrue(any('email_provider_verify' in sql for sql, _ in conn.cursor_value.calls))

    def test_duplicate_verification_reuses_task(self):
        conn = FakeConnection([self.brand, {'value': json.dumps(self.config)}, {'id': 51}])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.post({'action': 'verify', 'digest': self.module().config_digest(self.config)})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['existing'])
        self.assertEqual(conn.commits, 0)

    def test_parked_brand_cannot_access_provider(self):
        conn = FakeConnection([{**self.brand, 'lifecycle': 'soft_parked'}])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
