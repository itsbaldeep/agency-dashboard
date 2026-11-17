"""Exact-review withdrawal of owned static articles."""
import json
import re
from flask import Blueprint, jsonify, request
import models
from script_paths import ensure_agency_scripts
ensure_agency_scripts()
from publication_settings import project_destination, destination_digest

recovery = Blueprint('publication_recovery', __name__)
_HASH = re.compile(r'^[0-9a-f]{64}$')


def _object(value):
    if isinstance(value,dict):return value
    try:return json.loads(value or '{}')
    except (ValueError,TypeError):return {}


@recovery.route('/api/brands/<int:brand_id>/content/<int:content_id>/withdraw', methods=['GET','POST'])
def withdraw(brand_id,content_id):
    payload=request.get_json(silent=True) if request.method=='POST' else {}
    if not isinstance(payload,dict):return jsonify(ok=False,error='Review must be an object'),400
    conn=models.db()
    try:
        cur=conn.cursor()
        cur.execute('SELECT ci.id,ci.brand_id,ci.status,ci.publish_task_id,b.project_id,p.lifecycle FROM content_items ci JOIN brands b ON b.id=ci.brand_id LEFT JOIN projects p ON p.id=b.project_id WHERE ci.id=%s AND ci.brand_id=%s FOR UPDATE OF ci',(content_id,brand_id))
        item=cur.fetchone()
        if not item:return jsonify(ok=False,error='Article not found'),404
        if item['status']!='published' or item.get('lifecycle')!='active':return jsonify(ok=False,error='An active published article is required'),409
        config=project_destination(item.get('project_id'))
        if config.get('type')!='static' or not config.get('enabled'):return jsonify(ok=False,error='Withdrawal is available for a connected owned static blog'),409
        cur.execute("SELECT id,params,result_ref FROM tasks WHERE id=%s AND type='publish_content' AND status='done'",(item.get('publish_task_id'),))
        task=cur.fetchone()
        if not task:return jsonify(ok=False,error='Completed publication receipt required'),409
        receipt=_object(task.get('result_ref'));params=_object(task.get('params'));digest=destination_digest(config);manifest=receipt.get('manifest_hash','')
        if not isinstance(manifest,str) or not _HASH.fullmatch(manifest) or params.get('approved_destination')!=digest or str(receipt.get('brand_id'))!=str(brand_id) or str(receipt.get('content_id'))!=str(content_id):return jsonify(ok=False,error='Publication ownership or destination changed. Review the original task.'),409
        review={'brand_id':brand_id,'content_item_id':content_id,'publish_task_id':task['id'],'manifest_hash':manifest,'approved_destination':digest}
        if request.method=='GET':return jsonify(ok=True,review=review,url=receipt.get('url'),effect='Archive the exact published article and return its editorial item to draft. No email or social action.')
        if payload!=review:return jsonify(ok=False,error='Publication changed. Reload the exact review before confirming.'),409
        cur.execute("SELECT id FROM tasks WHERE type='static_publish_rollback' AND params->>'brand_id'=%s AND params->>'content_item_id'=%s AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",(str(brand_id),str(content_id)))
        active=cur.fetchone()
        if active:return jsonify(ok=True,task_id=active['id'],existing=True)
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('static_publish_rollback','queued',%s,'dashboard_exact_withdrawal_approval') RETURNING id",(json.dumps(review),))
        task_id=cur.fetchone()['id'];conn.commit();return jsonify(ok=True,task_id=task_id),201
    except Exception:
        conn.rollback();return jsonify(ok=False,error='Publication recovery review unavailable'),500
    finally:conn.close()
