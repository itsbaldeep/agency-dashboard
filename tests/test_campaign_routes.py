import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
import app as dashboard
from test_marketing_workspace import FakeConnection


class CampaignRouteTests(unittest.TestCase):
    def setUp(self):
        dashboard.app.config.update(TESTING=True)
        self.client = dashboard.app.test_client()
        self.item = {'id': 9, 'brand_id': 27, 'kind': 'email_campaign', 'channel': 'email',
                     'state': 'ready', 'revision': 3, 'brief': {}, 'body': 'Reviewed copy'}
        self.domain = mock.Mock()
        self.domain.validate_policy.side_effect = lambda p: p
        self.domain.default_policy.return_value = {'category': 'marketing'}
        self.url = '/api/brands/27/work-items/9/campaign'

    def post(self, payload, **kwargs):
        return self.client.post(self.url, json=payload, headers={'Origin': 'http://localhost'}, **kwargs)

    def test_missing_origin_cannot_save(self):
        with mock.patch.object(dashboard.models, 'db') as db:
            response = self.client.post(self.url, json={'revision': 3, 'policy': {}})
        self.assertEqual(response.status_code, 403)
        db.assert_not_called()

    def test_unknown_fields_cannot_import_recipient_data(self):
        with mock.patch.object(dashboard.models, 'db') as db:
            response = self.post({'revision': 3, 'policy': {}, 'recipients': ['private@example.com']})
        self.assertEqual(response.status_code, 400)
        db.assert_not_called()

    def test_stale_revision_cannot_replace_rules(self):
        conn = FakeConnection([self.item])
        with mock.patch('campaign_routes.policy_module', return_value=self.domain), mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.post({'revision': 2, 'policy': {'category': 'marketing'}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(conn.commits, 0)

    def test_policy_changes_reset_review_without_queueing(self):
        conn = FakeConnection([self.item, {'revision': 4}])
        with mock.patch('campaign_routes.policy_module', return_value=self.domain), mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.post({'revision': 3, 'policy': {'category': 'marketing'}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['sent'])
        self.assertFalse(response.json['scheduled'])
        queries = [sql for sql, _ in conn.cursor_value.calls]
        self.assertTrue(any("state='draft'" in sql for sql in queries))
        self.assertFalse(any('INSERT INTO tasks' in sql for sql in queries))
        self.assertEqual(conn.commits, 1)

    def test_ready_copy_does_not_imply_send_authority(self):
        conn = FakeConnection([self.item])
        with mock.patch('campaign_routes.policy_module', return_value=self.domain), mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['dispatch_available'])
        self.assertFalse(response.json['recipients_stored'])
        self.assertIn('Approve the exact message, audience and delivery time', response.json['blockers'])

    def test_wrong_brand_cannot_view_policy(self):
        conn = FakeConnection([])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.client.get('/api/brands/31/work-items/9/campaign')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(conn.cursor_value.calls[0][1], (9, 31))

    def test_social_item_cannot_be_used_as_email(self):
        conn = FakeConnection([{**self.item, 'kind': 'social_post'}])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)

    def test_malformed_legacy_brief_needs_repair(self):
        conn = FakeConnection([{**self.item, 'brief': '{broken'}])
        with mock.patch.object(dashboard.models, 'db', return_value=conn):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)

    def test_invalid_policy_rejected_before_database(self):
        self.domain.validate_policy.side_effect = ValueError('Unknown policy field')
        with mock.patch('campaign_routes.policy_module', return_value=self.domain), mock.patch.object(dashboard.models, 'db') as db:
            response = self.post({'revision': 3, 'policy': {'credentials': 'forbidden'}})
        self.assertEqual(response.status_code, 400)
        db.assert_not_called()
