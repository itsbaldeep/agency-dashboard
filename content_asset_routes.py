"""Project-scoped editorial asset library and revision-bound attachment."""
import json
import sys
from urllib.parse import urlsplit
from flask import Blueprint, jsonify, request
import models
from content_visuals import get_item, revision, error

assets = Blueprint('content_assets', __name__)


@assets.route('/content/<int:item_id>/assets/suggestions', methods=['GET', 'POST'])
def suggestions(item_id):
    """Reads never fetch or import assets. Explicit refresh creates bounded work."""
    conn = models.db()
    try:
        cur = conn.cursor()
        item = get_item(cur, item_id)
        from content_asset_workflow import fingerprint
        cur.execute("SELECT id,status FROM tasks WHERE type='content_asset_suggestions' "
                    "AND params->>'content_item_id'=%s ORDER BY id DESC LIMIT 1", (str(item_id),))
        task = cur.fetchone()
        if request.method == 'POST':
            if (request.get_json(silent=True) or {}).get('revision') != revision(item):
                raise PermissionError('Draft changed. Reload before refreshing image suggestions.')
            if not task or task['status'] not in ('queued', 'running'):
                cur.execute("INSERT INTO tasks(type,status,params,triggered_by) "
                            "VALUES ('content_asset_suggestions','queued',%s,'dashboard-image-suggestions') RETURNING id,status",
                            (json.dumps({'content_item_id': item_id, 'expected_fingerprint': fingerprint(item)}),))
                task = cur.fetchone()
            conn.commit()
            return jsonify(ok=True, task_id=task['id'], task_status=task['status'])
        report = (item.get('structured') or {}).get('asset_suggestions') or {}
        stale = bool(report) and report.get('fingerprint') != fingerprint(item)
        return jsonify(ok=True, slots=[] if stale else report.get('slots', []), stale=stale,
                       checked_at=report.get('checked_at'), revision=revision(item),
                       task_id=task['id'] if task else None, task_status=task['status'] if task else None)
    except (ValueError, PermissionError, LookupError) as exc:
        conn.rollback()
        return error(exc)
    finally:
        conn.rollback()
        conn.close()


@assets.post('/content/<int:item_id>/check-links')
def check_links(item_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        item = get_item(cur, item_id)
        if (request.get_json(silent=True) or {}).get('revision') != revision(item):
            raise PermissionError('Draft changed. Reload before checking links.')
        cur.execute("SELECT id FROM tasks WHERE type='content_link_check' AND status IN ('queued','running') "
                    "AND params->>'content_item_id'=%s ORDER BY id DESC LIMIT 1", (str(item_id),))
        existing = cur.fetchone()
        if existing:
            return jsonify(ok=True, task_id=existing['id'])
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('content_link_check','queued',%s,'dashboard-link-check') RETURNING id",
                    (json.dumps({'content_item_id': item_id}),))
        task_id = cur.fetchone()['id']
        conn.commit()
        return jsonify(ok=True, task_id=task_id)
    except (ValueError, PermissionError, LookupError) as exc:
        conn.rollback()
        return error(exc)
    finally:
        conn.close()


def module():
    sys.path.insert(0, '/home/agency/agency-os/scripts')
    import content_assets
    return content_assets


@assets.before_request
def same_site():
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if (origin and urlsplit(origin).netloc != request.host) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify(ok=False, error='Cross-site changes are not permitted'), 403


def record(cur, item, metadata, action):
    cur.execute('INSERT INTO content_assets(brand_id,content_item_id,sha256,metadata) VALUES (%s,%s,%s,%s) '
                'ON CONFLICT (brand_id,sha256) DO UPDATE SET sha256=EXCLUDED.sha256 RETURNING id,metadata',
                (item['brand_id'], item['id'], metadata['sha256'], json.dumps(metadata)))
    row = cur.fetchone()
    cur.execute("INSERT INTO tasks(type,status,params,triggered_by,result_ref,started_at,finished_at) "
                "VALUES ('content_asset_import','done',%s,'dashboard-asset-library',%s,now(),now()) RETURNING id",
                (json.dumps({'content_item_id': item['id'], 'action': action}),
                 json.dumps({'asset_id': row['id'], 'sha256': metadata['sha256'], 'publication': False})))
    return {**row['metadata'], 'id': row['id'], 'task_id': cur.fetchone()['id']}


@assets.get('/content/<int:item_id>/assets')
def library(item_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        item = get_item(cur, item_id)
        cur.execute('SELECT id,metadata FROM content_assets WHERE brand_id=%s ORDER BY id DESC LIMIT 60', (item['brand_id'],))
        return jsonify(ok=True, assets=[{**r['metadata'], 'id': r['id']} for r in cur.fetchall()])
    except (ValueError, PermissionError, LookupError) as exc:
        return error(exc)
    finally:
        conn.rollback()
        conn.close()


@assets.get('/content/<int:item_id>/assets/search')
def search(item_id):
    conn = models.db()
    try:
        get_item(conn.cursor(), item_id)
        query = request.args.get('q', '').strip()
        if not 3 <= len(query) <= 120:
            raise ValueError('Search needs 3 to 120 characters')
        return jsonify(ok=True, results=module().search_assets(query))
    except (ValueError, PermissionError, LookupError) as exc:
        return error(exc)
    finally:
        conn.rollback()
        conn.close()


@assets.post('/content/<int:item_id>/assets/upload')
@assets.post('/content/<int:item_id>/assets/import')
def upload(item_id):
    if request.content_length and request.content_length > 12_000_000:
        return jsonify(ok=False, error='Image exceeds 12 MB'), 413
    conn = models.db()
    try:
        cur = conn.cursor()
        item = get_item(cur, item_id)
        payload = request.get_json(silent=True) or request.form
        if payload.get('revision') != revision(item):
            raise PermissionError('Draft changed. Reload before importing an asset.')
        description = str(payload.get('description', '')).strip()[:500]
        if request.path.endswith('/import'):
            metadata = module().import_stock(int(payload.get('provider_id')), description=description)
            action = 'stock_import'
        else:
            uploaded = request.files.get('file')
            if payload.get('public_asset') not in (True, 'on', 'true'):
                raise ValueError('Confirm this image is safe for public storage. Do not upload private resumes or personal data.')
            if not uploaded or not description:
                raise ValueError('Choose an image and describe what it shows')
            rights = str(payload.get('rights', '')).strip()
            creator = str(payload.get('creator', '')).strip()
            if not creator or rights not in ('owned', 'licensed', 'generated'):
                raise ValueError('Confirm the creator and image rights')
            source = str(payload.get('source_url', '')).strip()
            if rights == 'licensed' and not source.startswith('https://'):
                raise ValueError('Licensed uploads require an HTTPS source or license URL')
            metadata = module().store_asset(uploaded.read(12_000_001), description,
                {'kind': rights, 'creator': creator[:200], 'source_url': source[:1000]})
            action = 'upload'
        result = record(cur, item, metadata, action)
        conn.commit()
        return jsonify(ok=True, asset=result)
    except (ValueError, TypeError, PermissionError, LookupError) as exc:
        conn.rollback()
        return error(exc)
    except Exception:
        conn.rollback()
        return jsonify(ok=False, error='The asset provider or storage could not complete this request. No image was attached; retry after checking service health.'), 503
    finally:
        conn.close()


@assets.post('/content/<int:item_id>/assets/attach')
def attach(item_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        item = get_item(cur, item_id)
        payload = request.get_json(silent=True) or {}
        if payload.get('revision') != revision(item):
            raise PermissionError('Draft changed. Reload before attaching an asset.')
        if payload.get('reviewed') is not True:
            raise ValueError('Review the image, alt text and provenance before attaching')
        cur.execute('SELECT id,metadata FROM content_assets WHERE id=%s AND brand_id=%s',
                    (payload.get('asset_id'), item['brand_id']))
        asset = cur.fetchone()
        if not asset:
            raise LookupError('Asset not found in this project')
        alt = str(payload.get('alt', '')).strip()
        if not 10 <= len(alt) <= 300:
            raise ValueError('Describe the image in 10 to 300 characters')
        index = payload.get('index')
        blocks = list(item['content_blocks'])
        if type(index) is not int or not 0 <= index <= len(blocks):
            raise ValueError('Choose a valid image position')
        metadata = {**asset['metadata'], 'id': asset['id'], 'alt': alt, 'reviewed': True,
                    'selection_source': 'dashboard_review'}
        block = {'type': 'image_slot', 'alt': alt, 'prompt': alt, 'url': metadata['url'], 'asset': metadata,
                 'reviewed': True, 'selection_source': 'dashboard_review',
                 'caption': str(payload.get('caption', '')).strip()[:500]}
        before = {key: item.get(key) for key in ('content_blocks', 'body', 'structured')}
        previous_block = None
        if index < len(blocks) and blocks[index].get('type') == 'image_slot':
            previous_block = blocks[index]
            block = {**blocks[index], **block}
            blocks[index] = block
        else:
            blocks.insert(index, block)
        from content_asset_workflow import replace_image_body
        body = replace_image_body(item.get('body') or '', previous_block, block)
        structured = dict(item.get('structured') or {})
        if 'visual_base_body' in structured:
            structured['visual_base_body'] = replace_image_body(structured['visual_base_body'], previous_block, block)
        from content_quality import validate_content
        structured['quality_report'] = validate_content(blocks, 'draft')
        structured.pop('link_report', None)
        cur.execute('UPDATE content_items SET content_blocks=%s,body=%s,structured=%s,updated_at=now() WHERE id=%s',
                    (json.dumps(blocks), body, json.dumps(structured), item_id))
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by,result_ref,started_at,finished_at) "
                    "VALUES ('content_asset_attach','done',%s,'dashboard-asset-review',%s,now(),now()) RETURNING id",
                    (json.dumps({'content_item_id': item_id, 'asset_id': asset['id']}),
                     json.dumps({'before': before, 'index': index, 'publication': False})))
        task_id = cur.fetchone()['id']
        conn.commit()
        return jsonify(ok=True, task_id=task_id)
    except (ValueError, TypeError, PermissionError, LookupError) as exc:
        conn.rollback()
        return error(exc)
    finally:
        conn.close()
