from browser_client import browser_client
import copy
import json
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, '/home/agency/core/agency-os/scripts')
import editorial_visuals
import app as dashboard
import content_visuals as workflow


def draft():
    return {'id': 23, 'title': 'Example', 'status': 'draft', 'lifecycle': 'active',
            'content_blocks': [{'type': 'prose', 'markdown': 'Original prose'}],
            'body': 'Original prose', 'structured': {'facts': []},
            'updated_at': datetime(2026, 9, 24, tzinfo=timezone.utc)}


VISUAL = {'kind': 'annotated_example', 'title': 'Make the action clear', 'caption': 'Use only facts you can support.',
          'before': 'Helped with onboarding', 'after': 'Maintained the onboarding checklist', 'notes': ['Name the work you actually did.']}


class Database:
    def __init__(self):
        self.item = draft()
        self.tasks = {}
        self.result = None
        self.commits = 0
        self.rollbacks = 0
        self.calls = []

    def cursor(self): return self
    def close(self): pass
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.startswith('SELECT ci.'):
            self.result = copy.deepcopy(self.item)
        elif sql.startswith('UPDATE content_items'):
            self.item.update(content_blocks=json.loads(params[0]), body=params[1], structured=json.loads(params[2]))
            self.item['updated_at'] += timedelta(seconds=1)
            self.result = {'updated_at': self.item['updated_at']}
        elif sql.startswith('INSERT INTO tasks'):
            task_id = len(self.tasks) + 500
            self.tasks[task_id] = {'params': json.loads(params[0]), 'result_ref': params[1]}
            self.result = {'id': task_id}
        elif sql.startswith('SELECT params,result_ref'):
            self.result = self.tasks.get(params[0])
        else:
            self.result = None

    def fetchone(self): return self.result


class VisualWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.client = browser_client(dashboard.app)
        self.patch = mock.patch.object(workflow.models, 'db', return_value=self.db)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def payload(self, **extra):
        return {'revision': workflow.revision(self.db.item), 'action': 'add', 'index': 1,
                'visual': VISUAL, **extra}

    def preview(self, payload):
        return self.client.post('/content/23/visuals/preview', json=payload)

    def save(self, payload=None):
        data = payload or self.payload()
        preview = self.preview(data)
        self.assertEqual(preview.status_code, 200, preview.get_json())
        return self.client.post('/content/23/visuals', json={**data, 'preview_digest': preview.get_json()['preview_digest']})

    def test_preview_is_non_mutating_and_escaped(self):
        response = self.preview(self.payload(visual={**VISUAL, 'title': '<script>bad</script>'}))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<script>', response.get_json()['html'])
        self.assertEqual(self.db.commits, 0)
        self.assertFalse(self.db.tasks)

    def test_save_preserves_prose_and_creates_revision(self):
        response = self.save()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.item['content_blocks'][0]['markdown'], 'Original prose')
        self.assertEqual(self.db.item['content_blocks'][1]['type'], 'editorial_visual')
        self.assertIn('Hypothetical example', self.db.item['body'])
        saved = json.loads(self.db.tasks[500]['result_ref'])
        self.assertEqual(saved['before']['body'], 'Original prose')
        self.assertFalse(saved['publication'])

    def test_stale_revision_and_missing_preview_rejected(self):
        self.assertEqual(self.preview(self.payload(revision='stale')).status_code, 409)
        self.assertEqual(self.client.post('/content/23/visuals', json=self.payload()).status_code, 400)
        self.assertFalse(self.db.tasks)

    def test_preview_cannot_authorize_changed_payload(self):
        payload = self.payload()
        preview = self.preview(payload).get_json()['preview_digest']
        response = self.client.post('/content/23/visuals', json={**payload, 'visual': {**VISUAL, 'after': 'Changed'}, 'preview_digest': preview})
        self.assertEqual(response.status_code, 400)

    def test_non_draft_and_inactive_rejected(self):
        for status in ['outline', 'publishing', 'approved', 'published']:
            self.db.item['status'] = status
            self.assertEqual(self.preview(self.payload()).status_code, 409)
        self.db.item['status'] = 'draft'
        self.db.item['lifecycle'] = 'soft_parked'
        self.assertEqual(self.preview(self.payload()).status_code, 409)

    def test_replace_remove_and_undo(self):
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.save(self.payload(action='replace', index=1, visual={**VISUAL, 'title':'Revised'})).status_code, 200)
        self.assertEqual(self.db.item['content_blocks'][1]['title'], 'Revised')
        self.assertEqual(self.save(self.payload(action='remove', index=1)).status_code, 200)
        self.assertEqual(len(self.db.item['content_blocks']), 1)
        response = self.client.post('/content/23/visuals/undo/502', json={'revision': workflow.revision(self.db.item)})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.db.item['content_blocks'][1]['title'], 'Revised')

    def test_undo_rejects_later_edits_and_form_posts(self):
        self.save()
        self.assertEqual(self.client.post('/content/23/visuals/undo/500').status_code, 409)
        self.save(self.payload(visual={**VISUAL, 'title':'Later'}))
        self.assertEqual(self.client.post('/content/23/visuals/undo/500', json={'revision':workflow.revision(self.db.item)}).status_code, 409)

    def test_malformed_input_and_stored_state_fail_closed(self):
        self.assertEqual(self.client.post('/content/23/visuals/preview',json=['bad']).status_code,400)
        for key, value in [('content_blocks', ['bad']), ('structured', 'bad'), ('body', None)]:
            self.db.item = draft(); self.db.item[key] = value
            self.assertEqual(self.preview(self.payload()).status_code,400)

    def test_no_facts_blocks_charts(self):
        visual = {'kind':'bar_chart','title':'Claim','caption':'Claim','units':'people',
                  'points':[{'label':'A','value':1,'fact_id':'missing'},{'label':'B','value':2,'fact_id':'missing'}]}
        self.assertEqual(self.preview(self.payload(visual=visual)).status_code,400)

    def test_position_and_visual_count_limits(self):
        self.assertEqual(self.preview(self.payload(index=99)).status_code,400)
        self.assertEqual(self.preview(self.payload(action='remove',index=0)).status_code,400)
        for _ in range(8): self.assertEqual(self.save().status_code,200)
        self.assertEqual(self.preview(self.payload()).status_code,400)

    def test_external_prose_edit_is_not_overwritten(self):
        self.save(); self.db.item['body'] += ' New editorial text'
        self.assertEqual(self.preview(self.payload()).status_code,409)

    def test_outline_preview_renders_briefs_not_blank(self):
        with dashboard.app.test_request_context('/'):
            item = {'id':23,'title':'Outline','status':'outline','structured':{'blocks':[{'type':'prose','brief':'An inspectable outline brief'}]}}
            result = dashboard._render_content_body(item,23,None,None)
            self.assertIn('An inspectable outline brief',result)


if __name__ == '__main__': unittest.main()
