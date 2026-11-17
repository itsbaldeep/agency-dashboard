"""Brand-owned email provider setup and tracked read-only verification."""
import json
from flask import Blueprint, jsonify, request
import models
from marketing_connections import _brand_project, _credential_path
from script_paths import ensure_agency_scripts
ensure_agency_scripts()

email_provider = Blueprint('email_provider', __name__)


def provider_module():
    import marketing_email_provider
    return marketing_email_provider


def _load(cur, brand_id, property_type):
    cur.execute('SELECT value FROM brand_properties WHERE brand_id=%s AND property_type=%s', (brand_id, property_type))
    row = cur.fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row.get('value') or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


@email_provider.route('/api/brands/<int:brand_id>/email-provider', methods=['GET', 'POST'])
def setup(brand_id):
    payload = request.get_json(silent=True) if request.method == 'POST' else {}
    if not isinstance(payload, dict):
        return jsonify(ok=False, error='Provider setup must be an object'), 400
    if request.content_length and request.content_length > 10000:
        return jsonify(ok=False, error='Request too large'), 413
    conn = models.db()
    try:
        cur = conn.cursor()
        brand = _brand_project(cur, brand_id, lock=request.method == 'POST')
        if not brand:
            return jsonify(ok=False, error='Brand not found'), 404
        if brand.get('lifecycle') != 'active' or brand.get('classification') not in ('core', 'engagement'):
            return jsonify(ok=False, error='An active owning ledger entry is required for provider credentials'), 409
        module = provider_module()
        current = _load(cur, brand_id, 'email_provider_config')
        try:
            current = module.validate_config(current) if current else {}
        except (ValueError, TypeError):
            current = {}
        digest = module.config_digest(current)
        if request.method == 'GET':
            verification = _load(cur, brand_id, 'email_provider_verification')
            safe = {key: verification[key] for key in ('status', 'authenticated', 'sender_verified', 'checked_at', 'error') if key in verification}
            if verification.get('config_digest') != digest:
                safe = {'status': 'not_verified'}
            return jsonify(ok=True, config=current, digest=digest, verification=safe, sending_enabled=False)
        action = payload.get('action')
        if action not in ('save', 'verify') or payload.get('digest') != digest:
            return jsonify(ok=False, error='Provider setup changed or action is invalid. Reload before continuing.'), 409
        if action == 'save':
            if set(payload) != {'action', 'digest', 'config'}:
                return jsonify(ok=False, error='Only provider references and sender identity may be saved'), 400
            try:
                config = module.validate_config(payload['config'])
                path = _credential_path(config['credential_ref'], brand)
                if not path:
                    raise ValueError('Credential reference must stay within its owning ledger root')
                config['credential_ref'] = path
            except (ValueError, TypeError, KeyError) as exc:
                return jsonify(ok=False, error=str(exc)), 400
            digest = module.config_digest(config)
            cur.execute("INSERT INTO brand_properties(brand_id,property_type,value,accessible) VALUES (%s,'email_provider_config',%s,false) ON CONFLICT(brand_id,property_type) DO UPDATE SET value=EXCLUDED.value,accessible=false,created_at=now()", (brand_id, json.dumps(config)))
            cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('email_provider_setup','done',%s,'dashboard') RETURNING id", (json.dumps({'brand_id': brand_id, 'config_digest': digest, 'provider': config['provider']}),))
            task_id = cur.fetchone()['id']
            conn.commit()
            return jsonify(ok=True, digest=digest, task_id=task_id, sending_enabled=False), 201
        if set(payload) != {'action', 'digest'}:
            return jsonify(ok=False, error='Verification accepts only the saved configuration digest'), 400
        if not current:
            return jsonify(ok=False, error='Save the provider references before verification'), 409
        cur.execute("SELECT id FROM tasks WHERE type='email_provider_verify' AND status IN ('queued','running') AND params->>'brand_id'=%s AND params->>'config_digest'=%s ORDER BY id DESC LIMIT 1", (str(brand_id), digest))
        active = cur.fetchone()
        if active:
            return jsonify(ok=True, task_id=active['id'], existing=True, sending_enabled=False)
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('email_provider_verify','queued',%s,'dashboard') RETURNING id", (json.dumps({'brand_id': brand_id, 'config_digest': digest}),))
        task_id = cur.fetchone()['id']
        conn.commit()
        return jsonify(ok=True, task_id=task_id, sending_enabled=False), 201
    finally:
        conn.close()
