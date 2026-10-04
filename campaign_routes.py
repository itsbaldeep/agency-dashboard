"""Revision-bound campaign policy, without recipient transfer or dispatch."""
import json
from flask import Blueprint, jsonify, request
import models
from script_paths import ensure_agency_scripts
ensure_agency_scripts()

campaigns = Blueprint('campaigns', __name__)


def policy_module():
    import marketing_campaigns
    return marketing_campaigns


@campaigns.route('/api/brands/<int:brand_id>/work-items/<int:item_id>/campaign', methods=['GET', 'POST'])
def campaign_policy(brand_id, item_id):
    payload = request.get_json(silent=True) if request.method == 'POST' else {}
    if not isinstance(payload, dict):
        return jsonify(ok=False, error='Campaign policy must be an object'), 400
    if request.method == 'POST':
        try:
            if set(payload) != {'revision', 'policy'} or type(payload['revision']) is not int or payload['revision'] < 1:
                raise ValueError('An exact draft revision and policy are required')
            policy = policy_module().validate_policy(payload['policy'])
        except (ValueError, TypeError) as exc:
            return jsonify(ok=False, error=str(exc)), 400
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s FOR UPDATE', (item_id, brand_id))
        item = cur.fetchone()
        if not item:
            return jsonify(ok=False, error='Campaign not found'), 404
        if item['kind'] != 'email_campaign' or item['channel'] != 'email' or item['state'] == 'archived':
            return jsonify(ok=False, error='An active email campaign draft is required'), 409
        brief = item.get('brief') or {}
        if not isinstance(brief, dict):
            try:
                brief = json.loads(brief)
            except (ValueError, TypeError):
                return jsonify(ok=False, error='Stored campaign brief needs repair before review'), 409
            if not isinstance(brief, dict):
                return jsonify(ok=False, error='Stored campaign brief needs repair before review'), 409
        if request.method == 'POST':
            if payload['revision'] != item['revision']:
                return jsonify(ok=False, error='Draft changed. Reload before saving campaign rules.'), 409
            brief = {**brief, 'campaign_policy': policy, 'email_category': policy['category']}
            cur.execute("UPDATE marketing_work_items SET brief=%s,state='draft',revision=revision+1,updated_at=now() WHERE id=%s AND brand_id=%s RETURNING revision", (json.dumps(brief), item_id, brand_id))
            revision = cur.fetchone()['revision']
            conn.commit()
            return jsonify(ok=True, revision=revision, sent=False, scheduled=False)
        policy = brief.get('campaign_policy')
        blockers = []
        if policy:
            try:
                policy = policy_module().validate_policy(policy)
            except (ValueError, TypeError):
                blockers.append('Saved campaign rules need review')
                policy = None
        if not policy:
            blockers.append('Save structured campaign rules')
            policy = policy_module().default_policy()
        if not str(item.get('body') or '').strip():
            blockers.append('Prepare campaign copy')
        if item['state'] != 'ready':
            blockers.append('Review the current copy and rules')
        blockers.extend(['Connect and verify a brand-owned delivery adapter',
                         'Preview consent, suppression and eligible audience at the source',
                         'Approve the exact message, audience and delivery time'])
        return jsonify(ok=True, revision=item['revision'], policy=policy, dispatch_available=False,
                       blockers=blockers, recipients_stored=False, scheduled=False)
    finally:
        conn.close()
