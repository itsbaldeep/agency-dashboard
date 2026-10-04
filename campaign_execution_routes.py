"""Brand-scoped campaign source, audience preview and exact human approval."""
import json
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
import models
from marketing_connections import _brand_project, _credential_path
from script_paths import ensure_agency_scripts
ensure_agency_scripts()

campaign_execution = Blueprint('campaign_execution', __name__)


def adapter_module():
    import marketing_campaign_adapter
    return marketing_campaign_adapter


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        value = json.loads(value or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _config(cur, brand_id):
    cur.execute("SELECT value FROM brand_properties WHERE brand_id=%s AND property_type='campaign_adapter_config'", (brand_id,))
    return _object((cur.fetchone() or {}).get('value'))


def _digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@campaign_execution.route('/api/brands/<int:brand_id>/campaign-source', methods=['GET', 'POST'])
def source_setup(brand_id):
    payload = request.get_json(silent=True) if request.method == 'POST' else {}
    if not isinstance(payload, dict):
        return jsonify(ok=False, error='Source setup must be an object'), 400
    conn = models.db()
    try:
        cur = conn.cursor()
        brand = _brand_project(cur, brand_id, lock=request.method == 'POST')
        if not brand:
            return jsonify(ok=False, error='Brand not found'), 404
        if brand.get('lifecycle') != 'active' or brand.get('classification') not in ('core', 'engagement'):
            return jsonify(ok=False, error='An active owning ledger entry is required'), 409
        config = _config(cur, brand_id)
        try:
            config = adapter_module().validate_config(config) if config else {}
        except (ValueError, TypeError):
            config = {}
        digest = _digest(config)
        if request.method == 'GET':
            return jsonify(ok=True, config=config, digest=digest, connected=False,
                           note='A saved reference does not prove the adapter implements audience preview and delivery.')
        if set(payload) != {'digest', 'config'} or payload['digest'] != digest:
            return jsonify(ok=False, error='Source configuration changed. Reload before saving.'), 409
        try:
            config = adapter_module().validate_config(payload['config'])
            path = _credential_path(config['credential_ref'], brand)
            if not path:
                raise ValueError('Credential reference must stay within its owning ledger root')
            config['credential_ref'] = path
        except (ValueError, TypeError, KeyError):
            return jsonify(ok=False, error='Use a localhost origin, an owned credential reference and an uppercase variable name'), 400
        cur.execute("INSERT INTO brand_properties(brand_id,property_type,value,accessible) VALUES (%s,'campaign_adapter_config',%s,false) ON CONFLICT(brand_id,property_type) DO UPDATE SET value=EXCLUDED.value,accessible=false,created_at=now()", (brand_id, json.dumps(config)))
        if _digest(config) != digest:
            cur.execute("UPDATE marketing_work_items SET brief=brief-'campaign_preview',updated_at=now() WHERE brand_id=%s AND kind='email_campaign'", (brand_id,))
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('campaign_source_setup','done',%s,'dashboard') RETURNING id", (json.dumps({'brand_id': brand_id, 'config_digest': _digest(config)}),))
        task_id = cur.fetchone()['id']
        conn.commit()
        return jsonify(ok=True, digest=_digest(config), task_id=task_id, sending_enabled=False), 201
    finally:
        conn.close()


def _load_item(cur, brand_id, item_id):
    cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s FOR UPDATE', (item_id, brand_id))
    item = cur.fetchone()
    if item:
        item = dict(item)
        item['brief'] = _object(item.get('brief'))
        if item.get('planned_at'):
            item['planned_at'] = item['planned_at'].isoformat()
    return item


@campaign_execution.route('/api/brands/<int:brand_id>/work-items/<int:item_id>/campaign-execution', methods=['GET', 'POST'])
def execution(brand_id, item_id):
    payload = request.get_json(silent=True) if request.method == 'POST' else {}
    if not isinstance(payload, dict):
        return jsonify(ok=False, error='Campaign review must be an object'), 400
    conn = models.db()
    try:
        cur = conn.cursor()
        item = _load_item(cur, brand_id, item_id)
        if not item:
            return jsonify(ok=False, error='Campaign not found'), 404
        if item['kind'] != 'email_campaign' or item['channel'] != 'email' or item['state'] == 'archived':
            return jsonify(ok=False, error='An active email campaign is required'), 409
        brand = _brand_project(cur, brand_id)
        if not brand or brand.get('lifecycle') != 'active' or brand.get('classification') not in {'core', 'engagement'}:
            return jsonify(ok=False, error='Owning ledger entry must be active'), 409
        config = _config(cur, brand_id)
        if request.method == 'GET':
            cur.execute('SELECT id,revision,state,send_at,task_id,receipt,error FROM marketing_campaign_runs WHERE brand_id=%s AND item_id=%s ORDER BY id DESC LIMIT 20', (brand_id, item_id))
            runs = [dict(row) for row in cur.fetchall()]
            for row in runs:
                if row.get('send_at'):
                    row['send_at'] = row['send_at'].isoformat()
            preview = item['brief'].get('campaign_preview')
            return jsonify(ok=True, revision=item['revision'], configured=bool(config), preview=preview,
                           reviewed=item['state'] == 'ready', runs=runs)
        action = payload.get('action')
        if action not in ('preview', 'review', 'approve', 'cancel', 'receipt'):
            return jsonify(ok=False, error='Unsupported campaign action'), 400
        if action in ('cancel', 'receipt'):
            if set(payload) != {'action', 'run_id'} or type(payload['run_id']) is not int:
                return jsonify(ok=False, error='An exact campaign run is required'), 400
            cur.execute('SELECT id,state,task_id FROM marketing_campaign_runs WHERE id=%s AND brand_id=%s AND item_id=%s FOR UPDATE', (payload['run_id'], brand_id, item_id))
            run = cur.fetchone()
            if not run:
                return jsonify(ok=False, error='Campaign run not found'), 404
            if action == 'cancel':
                if run['state'] not in ('approved', 'queued'):
                    return jsonify(ok=False, error='In-flight delivery cannot be cancelled. Check the source receipt.'), 409
                cur.execute("UPDATE marketing_campaign_runs SET state='cancelled',updated_at=now() WHERE id=%s", (run['id'],))
                conn.commit()
                return jsonify(ok=True, cancelled=True)
            if run['state'] in ('approved', 'queued', 'cancelled'):
                return jsonify(ok=False, error='Delivery has not started; there is no source receipt to reconcile'), 409
            cur.execute("SELECT id FROM tasks WHERE type='marketing_campaign_receipt' AND status IN ('queued','running') AND params->>'run_id'=%s", (str(run['id']),))
            pending = cur.fetchone()
            if pending:
                return jsonify(ok=True, task_id=pending['id'])
            cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('marketing_campaign_receipt','queued',%s,'dashboard') RETURNING id", (json.dumps({'brand_id': brand_id, 'run_id': run['id']}),))
            task_id = cur.fetchone()['id']
            conn.commit()
            return jsonify(ok=True, task_id=task_id, sent=False), 201
        if type(payload.get('revision')) is not int or payload['revision'] != item['revision']:
            return jsonify(ok=False, error='Draft changed. Reload the current campaign.'), 409
        if not config:
            return jsonify(ok=False, error='Configure a brand-owned campaign source first'), 409
        if action == 'preview':
            if set(payload) != {'action', 'revision'}:
                return jsonify(ok=False, error='Preview accepts only the exact draft revision'), 400
            try:
                adapter_module().validate_config(config)
                adapter_module().preview_request(item, datetime.now(timezone.utc))
            except (ValueError, TypeError):
                return jsonify(ok=False, error='Complete campaign copy, subject and rules before preview'), 409
            cur.execute("SELECT id FROM tasks WHERE type='marketing_campaign_preview' AND status IN ('queued','running') AND params->>'brand_id'=%s AND params->>'item_id'=%s AND params->>'revision'=%s", (str(brand_id), str(item_id), str(item['revision'])))
            pending = cur.fetchone()
            if pending:
                return jsonify(ok=True, task_id=pending['id'])
            cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('marketing_campaign_preview','queued',%s,'dashboard') RETURNING id", (json.dumps({'brand_id': brand_id, 'item_id': item_id, 'revision': item['revision']}),))
            task_id = cur.fetchone()['id']
            conn.commit()
            return jsonify(ok=True, task_id=task_id, sent=False), 201
        if set(payload) != ({'action', 'revision', 'send_at'} if action == 'review' else {'action', 'revision', 'send_at', 'approval_digest'}):
            return jsonify(ok=False, error='An exact message, audience and send time review is required'), 400
        try:
            send_at = datetime.fromisoformat(str(payload['send_at']).replace('Z', '+00:00'))
            contract = adapter_module().approval_contract(item, config, item['brief'].get('campaign_preview'), send_at, datetime.now(timezone.utc))
        except (ValueError, TypeError, KeyError):
            return jsonify(ok=False, error='Review the draft and collect a fresh eligible audience preview. Choose a time before its expiry.'), 409
        if action == 'review':
            return jsonify(ok=True, review=contract, effect='Authorize only this message and frozen audience at the specified time. Fresh source checks may remove recipients.' )
        if payload['approval_digest'] != contract['approval_digest']:
            return jsonify(ok=False, error='The exact review changed. Review it again before approval.'), 409
        cur.execute("INSERT INTO marketing_campaign_runs(brand_id,item_id,revision,approval_digest,idempotency_key,contract,state,send_at) VALUES (%s,%s,%s,%s,%s,%s,'approved',%s) ON CONFLICT(approval_digest) DO NOTHING RETURNING id", (brand_id, item_id, item['revision'], contract['approval_digest'], contract['idempotency_key'], json.dumps(contract), send_at))
        row = cur.fetchone()
        if not row:
            cur.execute('SELECT id,state FROM marketing_campaign_runs WHERE approval_digest=%s AND brand_id=%s AND item_id=%s', (contract['approval_digest'], brand_id, item_id))
            row = cur.fetchone()
        conn.commit()
        state = row.get('state', 'approved')
        return jsonify(ok=True, run_id=row['id'], state=state, scheduled=state in ('approved', 'queued'), sent=False), 201
    finally:
        conn.close()
