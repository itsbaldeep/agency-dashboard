"""Opt-in rollback-only SQL proof of the tracked editorial save contract."""
import json
import os
import unittest
from unittest.mock import patch

import psycopg2
import psycopg2.extras
import app as dashboard
from browser_client import browser_client


@unittest.skipUnless(os.environ.get('AGENCY_CONTENT_EDIT_DB_TEST') == '1', 'opt-in temporary PostgreSQL integration')
class ContentEditPostgresTests(unittest.TestCase):
    def test_revision_and_audit_are_atomic(self):
        conn = psycopg2.connect(host=os.environ.get('AGENCY_TEST_DB_HOST','100.64.0.1'),dbname='agencyos',user='agency',password=os.environ['AGENCY_TEST_DB_PASSWORD'])
        class Session:
            def cursor(self): return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass
        try:
            with conn.cursor() as cur:
                cur.execute('''SET LOCAL search_path=pg_temp;
                CREATE TEMP TABLE projects(id integer PRIMARY KEY,lifecycle text);
                CREATE TEMP TABLE brands(id integer PRIMARY KEY,project_id integer);
                CREATE TEMP TABLE content_items(id integer PRIMARY KEY,brand_id integer,title text,status text,content_blocks jsonb,body text,structured jsonb,approval_id integer,publish_task_id integer,updated_at timestamptz DEFAULT now());
                CREATE TEMP TABLE tasks(id serial PRIMARY KEY,type text,status text,params jsonb,triggered_by text,result_ref text,started_at timestamptz,finished_at timestamptz);
                INSERT INTO projects VALUES(1,'active'); INSERT INTO brands VALUES(1,1);''')
                cur.execute('INSERT INTO content_items(id,brand_id,title,status,content_blocks,body,structured) VALUES(1,1,%s,%s,%s,%s,%s)',('Temporary article','draft',json.dumps([{'type':'prose','markdown':'Original text'}]),'Original text',json.dumps({'facts':[],'link_report':{'old':True}})))
            with patch('models.db',return_value=Session()):
                client=browser_client(dashboard.app)
                original=client.get('/content/1/edit-data').get_json()
                payload={'revision':original['revision'],'blocks':[{'type':'prose','markdown':'Corrected **text**'}],'metadata':{'meta_description':'A truthful description'},'review_note':'Correct a source-grounded instruction'}
                preview=client.post('/content/1/edit/preview',json=payload)
                self.assertEqual(preview.status_code,200,preview.get_json())
                with conn.cursor() as cur:
                    cur.execute('SELECT count(*) FROM tasks'); self.assertEqual(cur.fetchone()[0],0)
                saved=client.post('/content/1/edit',json={**payload,'preview_digest':preview.get_json()['preview_digest']})
                self.assertEqual(saved.status_code,200,saved.get_json())
                self.assertNotEqual(saved.get_json()['revision'],original['revision'])
                stale=client.post('/content/1/edit',json={**payload,'preview_digest':preview.get_json()['preview_digest']})
                self.assertEqual(stale.status_code,409)
                with conn.cursor() as cur:
                    cur.execute('SELECT body,structured FROM content_items WHERE id=1'); body,structured=cur.fetchone()
                    self.assertEqual(body,'Corrected **text**'); self.assertNotIn('link_report',structured)
                    cur.execute('SELECT count(*) FROM tasks'); self.assertEqual(cur.fetchone()[0],1)
                    cur.execute('SELECT result_ref FROM tasks'); audit=json.loads(cur.fetchone()[0])
                    self.assertEqual(audit['before']['body'],'Original text'); self.assertFalse(audit['publication'])
        finally:
            conn.rollback(); conn.close()
