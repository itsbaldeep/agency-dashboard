"""Reviewed, reversible visual edits to composed drafts. No publication or models."""
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
import models
from script_paths import ensure_agency_scripts

content_visuals = Blueprint('content_visuals', __name__)


def visual_module():
    ensure_agency_scripts()
    import editorial_visuals
    return editorial_visuals


def revision(item):
    return hashlib.sha256(json.dumps({key: item.get(key) for key in
        ('title', 'status', 'content_blocks', 'body', 'structured', 'updated_at')},
        sort_keys=True, default=str).encode()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def get_item(cur, item_id):
    cur.execute('SELECT ci.*, p.lifecycle FROM content_items ci JOIN brands b ON b.id=ci.brand_id '
                'JOIN projects p ON p.id=b.project_id WHERE ci.id=%s FOR UPDATE OF ci', (item_id,))
    item = cur.fetchone()
    if not item:
        raise LookupError('Content item not found')
    if item['status'] != 'draft' or item.get('lifecycle') != 'active':
        raise PermissionError('Only composed drafts in active engagements can be edited')
    if (not isinstance(item.get('content_blocks'), list) or not item['content_blocks']
            or not all(isinstance(b, dict) for b in item['content_blocks'])):
        raise ValueError('This draft has no typed blocks to edit')
    if not item.get('body'):
        raise ValueError('Complete or restore the article body before editing visuals')
    structured = item.get('structured') or {}
    if not isinstance(structured, dict):
        raise ValueError('Draft metadata must be a structured object')
    if len(json.dumps(item, default=str)) > 300000:
        raise ValueError('Draft exceeds the bounded visual revision size')
    if 'visual_base_body' in structured:
        expected = structured['visual_base_body'] + ''.join('\n\n' + visual_module().visual_markdown(b)
            for b in item['content_blocks'] if b.get('type') == 'editorial_visual')
        if item['body'] != expected:
            raise PermissionError('The prose changed outside visual editing. Reconcile the draft before adding visuals.')
    return item


def candidate(item, payload):
    if not isinstance(payload, dict):
        raise ValueError('Visual edit must be a JSON object')
    if payload.get('revision') != revision(item):
        raise PermissionError('Draft changed. Reload and preview the edit again.')
    blocks = item['content_blocks']
    action = payload.get('action', 'add')
    if action not in ('add', 'replace', 'remove'):
        raise ValueError('Unknown visual action')
    index = payload.get('index')
    if type(index) is not int or index < 0 or index > len(blocks):
        raise ValueError('Choose a valid insertion position')
    if action != 'add' and (index == len(blocks) or blocks[index].get('type') != 'editorial_visual'):
        raise ValueError('Only reviewed visuals can be replaced or removed here')
    if action == 'add' and sum(b.get('type') == 'editorial_visual' for b in blocks) >= 8:
        raise ValueError('Maximum eight added visuals per article. Keep each one useful.')
    visual = None
    if action != 'remove':
        visual = visual_module().validate_visual(payload.get('visual'), (item.get('structured') or {}).get('facts') or [])
    result = {'action': action, 'index': index, 'visual': visual, 'revision': revision(item)}
    return result, digest(result)


def error(exc):
    return jsonify(ok=False, error=str(exc)), 404 if isinstance(exc, LookupError) else 409 if isinstance(exc, PermissionError) else 400


@content_visuals.post('/content/<int:item_id>/visuals/preview')
def preview_visual(item_id):
    if request.content_length and request.content_length > 40000:
        return jsonify(ok=False, error='Visual payload exceeds 40 KB'), 413
    conn = models.db()
    try:
        item = get_item(conn.cursor(), item_id)
        edit, fingerprint = candidate(item, request.get_json(silent=True) or {})
        html = visual_module().render_visual(edit['visual']) if edit['visual'] else '<p>This visual will be removed. The prose is unchanged.</p>'
        return jsonify(ok=True, html=html, preview_digest=fingerprint)
    except (ValueError, PermissionError, LookupError) as exc:
        return error(exc)
    finally:
        conn.rollback()
        conn.close()


@content_visuals.post('/content/<int:item_id>/visuals')
def save_visual(item_id):
    if request.content_length and request.content_length > 40000:
        return jsonify(ok=False, error='Visual payload exceeds 40 KB'), 413
    payload = request.get_json(silent=True) or {}
    conn = models.db()
    try:
        cur = conn.cursor()
        item = get_item(cur, item_id)
        edit, fingerprint = candidate(item, payload)
        if payload.get('preview_digest') != fingerprint:
            raise ValueError('Preview this exact edit before saving it')
        before = {key: item.get(key) for key in ('content_blocks', 'body', 'structured')}
        blocks = list(item['content_blocks'])
        visual = edit['visual']
        if visual:
            visual = {**visual, 'visual_id': uuid.uuid4().hex, 'reviewed': True}
        if edit['action'] == 'add':
            blocks.insert(edit['index'], visual)
        elif edit['action'] == 'replace':
            blocks[edit['index']] = visual
        else:
            blocks.pop(edit['index'])
        structured = dict(item.get('structured') or {})
        # Preserve the exact prose body; portable visual descriptions are appended
        # in Markdown, while HTML export preserves the chosen in-article position.
        original = structured.setdefault('visual_base_body', item.get('body') or '')
        body = original + ''.join('\n\n' + visual_module().visual_markdown(b) for b in blocks if b.get('type') == 'editorial_visual')
        structured['visual_review'] = {'status': 'reviewed', 'at': datetime.now(timezone.utc).isoformat(), 'count': sum(b.get('type') == 'editorial_visual' for b in blocks)}
        cur.execute("UPDATE content_items SET content_blocks=%s,body=%s,structured=%s,updated_at=now() WHERE id=%s RETURNING updated_at",
                    (json.dumps(blocks), body, json.dumps(structured), item_id))
        updated_at = cur.fetchone()['updated_at']
        after = {**item, 'content_blocks': blocks, 'body': body, 'structured': structured, 'updated_at': updated_at}
        result = {'before': before, 'after_revision': revision(after), 'action': edit['action'], 'publication': False}
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by,result_ref,started_at,finished_at) "
                    "VALUES ('content_visual_edit','done',%s,'dashboard-visual-review',%s,now(),now()) RETURNING id",
                    (json.dumps({'content_item_id': item_id, 'kind': visual.get('kind') if visual else 'remove'}), json.dumps(result)))
        task_id = cur.fetchone()['id']
        conn.commit()
        return jsonify(ok=True, task_id=task_id, revision=revision(after))
    except (ValueError, PermissionError, LookupError) as exc:
        conn.rollback()
        return error(exc)
    finally:
        conn.close()


@content_visuals.post('/content/<int:item_id>/visuals/undo/<int:task_id>')
def undo_visual(item_id, task_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        item = get_item(cur, item_id)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get('revision') != revision(item):
            raise PermissionError('Reload the current draft before undoing a visual edit')
        cur.execute("SELECT params,result_ref FROM tasks WHERE id=%s AND type='content_visual_edit' AND status='done'", (task_id,))
        task = cur.fetchone()
        if not task or not isinstance(task.get('params'), dict) or task['params'].get('content_item_id') != item_id:
            raise LookupError('Visual revision not found for this draft')
        saved = json.loads(task['result_ref']) if isinstance(task['result_ref'], str) else task['result_ref']
        if not isinstance(saved, dict):
            raise ValueError('Visual revision snapshot is malformed')
        if saved.get('after_revision') != revision(item):
            raise PermissionError('A later edit exists. Undo cannot overwrite subsequent work.')
        before = saved['before']
        if not isinstance(before, dict) or not isinstance(before.get('content_blocks'), list) or not isinstance(before.get('structured'), dict) or not isinstance(before.get('body'), str):
            raise ValueError('Visual revision snapshot is incomplete')
        cur.execute('UPDATE content_items SET content_blocks=%s,body=%s,structured=%s,updated_at=now() WHERE id=%s',
                    (json.dumps(before['content_blocks']), before['body'], json.dumps(before['structured']), item_id))
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by,result_ref,started_at,finished_at) "
                    "VALUES ('content_visual_undo','done',%s,'dashboard-visual-review',%s,now(),now()) RETURNING id",
                    (json.dumps({'content_item_id': item_id, 'undo_task_id': task_id}), json.dumps({'restored': True, 'publication': False})))
        undo_id = cur.fetchone()['id']
        conn.commit()
        return jsonify(ok=True, task_id=undo_id)
    except (ValueError, PermissionError, LookupError, TypeError) as exc:
        conn.rollback()
        return error(exc)
    finally:
        conn.close()
