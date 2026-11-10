import json
import unittest
from unittest import mock
import app as dashboard
import publication_recovery_routes as routes
from browser_client import browser_client
from test_marketing_workspace import FakeConnection


class PublicationRecoveryRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client=browser_client(dashboard.app)
        self.destination={'type':'static','enabled':True,'base_url':'https://example.test/journal','output_root':'/brand/7'}
        self.digest=routes.destination_digest(self.destination)
        self.item={'id':9,'brand_id':7,'status':'published','publish_task_id':20,'project_id':3,'lifecycle':'active'}
        self.task={'id':20,'params':{'approved_destination':self.digest},'result_ref':json.dumps({'brand_id':'7','content_id':'9','manifest_hash':'a'*64,'url':'https://example.test/journal/article/9/'})}
        self.review={'brand_id':7,'content_item_id':9,'publish_task_id':20,'manifest_hash':'a'*64,'approved_destination':self.digest}

    def test_other_brand_cannot_review_or_queue_withdrawal(self):
        conn=FakeConnection([None])
        with mock.patch.object(dashboard.models,'db',return_value=conn):
            response=self.client.post('/api/brands/8/content/9/withdraw',json=self.review)
        self.assertEqual(response.status_code,404)
        self.assertEqual(conn.commits,0)
        self.assertIn('ci.brand_id=%s',conn.cursor_value.calls[0][0])

    def test_exact_review_queues_one_tracked_task(self):
        conn=FakeConnection([self.item,self.task,None,{'id':45}])
        with mock.patch.object(dashboard.models,'db',return_value=conn),mock.patch.object(routes,'project_destination',return_value=self.destination):
            response=self.client.post('/api/brands/7/content/9/withdraw',json=self.review)
        self.assertEqual(response.status_code,201)
        self.assertEqual(response.json['task_id'],45)
        self.assertEqual(conn.commits,1)

    def test_changed_receipt_or_config_rejects_before_queue(self):
        conn=FakeConnection([self.item,{**self.task,'params':{'approved_destination':'changed'}}])
        with mock.patch.object(dashboard.models,'db',return_value=conn),mock.patch.object(routes,'project_destination',return_value=self.destination):
            response=self.client.get('/api/brands/7/content/9/withdraw')
        self.assertEqual(response.status_code,409)
        self.assertEqual(conn.commits,0)

    def test_confirmation_must_match_current_review(self):
        conn=FakeConnection([self.item,self.task])
        with mock.patch.object(dashboard.models,'db',return_value=conn),mock.patch.object(routes,'project_destination',return_value=self.destination):
            response=self.client.post('/api/brands/7/content/9/withdraw',json={**self.review,'manifest_hash':'b'*64})
        self.assertEqual(response.status_code,409)
        self.assertEqual(conn.commits,0)
