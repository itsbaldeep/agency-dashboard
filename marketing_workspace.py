"""Brand-scoped marketing workspace, planning and review surfaces."""
import errno
import hashlib
import io
import json
import os
import stat
import csv
import zipfile
import re
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, jsonify, redirect, abort, make_response, send_file
import models
from script_paths import ensure_agency_scripts
ensure_agency_scripts()
import marketing_channel_identity as channel_identity

marketing = Blueprint('marketing', __name__)
TABS = ('overview', 'strategy', 'measurement', 'editorial', 'social', 'lifecycle', 'setup')


def _upload_directory_fd(base, brand_id, item_id):
    """Open the brand upload directory without following directory symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
    # Validate the fixed root's existing ancestors before opening it. Newly
    # created children are traversed only through directory descriptors.
    current = Path(base)
    ancestors = list(current.parents)[::-1] + [current]
    for path in ancestors:
        try:
            if path.is_symlink():
                raise ValueError('Upload path is not a safe brand-owned directory')
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise

    parent_fd = os.open(str(Path(base).parent), flags)
    fds = [parent_fd]

    def child(parent, name):
        created = False
        try:
            fd = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            os.mkdir(name, 0o2770, dir_fd=parent)
            created = True
            fd = os.open(name, flags, dir_fd=parent)
        if created:
            os.fchmod(fd, 0o2770)
        fds.append(fd)
        return fd

    try:
        brands_fd = child(parent_fd, Path(base).name)
        brand_fd = child(brands_fd, str(brand_id))
        child(brand_fd, 'inputs')
        item_fd = child(fds[-1], str(item_id))
        return item_fd, fds
    except Exception:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _store_upload_bytes(directory_fd, filename, data):
    """Create a leaf safely, accepting an identical existing regular file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(filename, flags, 0o640, dir_fd=directory_fd)
    except FileExistsError:
        read_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(filename, read_flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ValueError('Upload destination is not a safe regular file') from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_size > len(data):
                raise ValueError('Upload destination is not a safe regular file')
            existing = b''
            while True:
                block = os.read(fd, 1024 * 1024)
                if not block:
                    break
                existing += block
            if existing != data:
                raise ValueError('Upload destination already contains different bytes')
        finally:
            os.close(fd)
        return False
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return True

@marketing.before_request
def protect_writes():
    if request.method == 'POST':
        origin = request.headers.get('Origin')
        if origin != request.host_url.rstrip('/'):
            return jsonify(ok=False,error='Cross-origin write rejected'),403
        limit = 13*1024*1024 if request.path.endswith('/ui-asset') else 100000
        if request.content_length and request.content_length > limit:
            return jsonify(ok=False,error='Request too large'),413

def domain_module():
    import marketing_studio
    return marketing_studio

def _object(value):
    if isinstance(value, dict): return value
    try: return json.loads(value or '{}')
    except (ValueError, TypeError): return {}

def portfolio_rows():
    return [e for e in models.get_engagements() if e.get('brand_id')]

@marketing.route('/brands')
def brands():
    return redirect('/dashboard', code=302)

@marketing.route('/reports')
def reports():
    return render_template('marketing_reports.html', brands=portfolio_rows())

@marketing.route('/administration')
def administration():
    return render_template('marketing_admin.html')

@marketing.route('/brands/<int:brand_id>')
@marketing.route('/brands/<int:brand_id>/<tab>')
def workspace(brand_id, tab='overview'):
    if tab not in TABS: abort(404)
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM brands WHERE id=%s', (brand_id,))
        brand = cur.fetchone()
        if not brand: abort(404)
        cur.execute('SELECT property_type,value FROM brand_properties WHERE brand_id=%s', (brand_id,))
        properties = {r['property_type']: r['value'] for r in cur.fetchall()}
        cur.execute('SELECT * FROM marketing_brand_profiles WHERE brand_id=%s', (brand_id,))
        saved = cur.fetchone() or {}
        profile = _object(saved.get('profile'))
        cur.execute('SELECT * FROM marketing_channels WHERE brand_id=%s ORDER BY channel', (brand_id,))
        channels = {r['channel']: dict(r) for r in cur.fetchall()}
        cur.execute('SELECT enabled FROM marketing_measurement_schedules WHERE brand_id=%s',(brand_id,))
        schedule_enabled = bool((cur.fetchone() or {}).get('enabled'))
        cur.execute("SELECT * FROM marketing_work_items WHERE brand_id=%s AND state<>'archived' ORDER BY updated_at DESC LIMIT 100", (brand_id,))
        items = cur.fetchall()
        cur.execute("SELECT id,audit_type,summary,raw_data,created_at FROM audits WHERE brand_id=%s ORDER BY created_at DESC LIMIT 12", (brand_id,))
        audits = cur.fetchall()
        cur.execute('SELECT id,title,status,content_type,updated_at FROM content_items WHERE brand_id=%s ORDER BY updated_at DESC LIMIT 30', (brand_id,))
        content = cur.fetchall()
        cur.execute("SELECT id,title,rationale,impact,status FROM suggestions WHERE brand_id=%s AND status='pending' ORDER BY id DESC LIMIT 15", (brand_id,))
        suggestions = cur.fetchall()
        cur.execute("SELECT id,type,status,created_at,error FROM tasks WHERE params->>'brand_id'=%s ORDER BY id DESC LIMIT 12", (str(brand_id),))
        tasks = cur.fetchall()
    finally: conn.close()
    seo = next((a for a in audits if a['audit_type']=='seo_measurement'), None)
    evidence = _object(seo.get('raw_data')) if seo else {}
    from app import _normalise_growth_report, _normalise_activation_report
    growth_report = _normalise_growth_report(evidence)
    activation = _normalise_activation_report(evidence)
    sources = evidence.get('sources') if isinstance(evidence.get('sources'), dict) else {}
    from core_enquiries import configured_for_brand
    enquiry_source_configured = configured_for_brand(brand['id'], brand.get('project_id'))
    return render_template('marketing_workspace.html', brand=brand, profile=profile,
        profile_revision=saved.get('revision', 0), properties=properties, tab=tab, tabs=TABS,
        items=items, audits=audits, content_items=content, suggestions=suggestions, tasks=tasks,
        seo=seo, evidence=evidence, sources=sources, growth_report=growth_report,
        activation_report=activation, plays=domain_module().GTM_PLAYBOOKS, channels=channels,
        schedule_enabled=schedule_enabled, enquiry_source_configured=enquiry_source_configured)

@marketing.route('/api/brands/<int:brand_id>/profile', methods=['POST'])
def profile_save(brand_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict): return jsonify(ok=False,error='Profile must be an object'),400
    try:
        revision = int(payload.get('revision', 0))
        profile = domain_module().validate_profile(payload.get('profile', {}))
    except (ValueError, TypeError) as exc: return jsonify(ok=False,error=str(exc)),400
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id FROM brands WHERE id=%s FOR UPDATE', (brand_id,))
        if not cur.fetchone(): return jsonify(ok=False,error='Brand not found'),404
        cur.execute('SELECT revision FROM marketing_brand_profiles WHERE brand_id=%s', (brand_id,))
        row = cur.fetchone()
        if (row['revision'] if row else 0) != revision:
            return jsonify(ok=False,error='Brand brief changed. Reload before saving.'),409
        cur.execute('''INSERT INTO marketing_brand_profiles (brand_id,profile) VALUES (%s,%s)
            ON CONFLICT (brand_id) DO UPDATE SET profile=EXCLUDED.profile,
            revision=marketing_brand_profiles.revision+1,updated_at=now() RETURNING revision''', (brand_id,json.dumps(profile)))
        new_revision=cur.fetchone()['revision']; conn.commit()
        return jsonify(ok=True,revision=new_revision)
    finally: conn.close()

@marketing.route('/api/brands/<int:brand_id>/work-items', methods=['POST'])
def work_create(brand_id):
    try:
        payload = request.get_json(silent=True) or {}
        payload['brand_id'] = brand_id
        item=domain_module().validate_work_item(payload)
    except (ValueError,TypeError) as exc: return jsonify(ok=False,error=str(exc)),400
    conn=models.db()
    try:
        cur=conn.cursor(); cur.execute('SELECT id FROM brands WHERE id=%s', (brand_id,))
        if not cur.fetchone(): return jsonify(ok=False,error='Brand not found'),404
        cur.execute('''INSERT INTO marketing_work_items (brand_id,kind,channel,title,brief,body,planned_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (brand_id,item['kind'],item['channel'],item['title'],json.dumps(item.get('brief',{})),item.get('body',''),item.get('planned_at')))
        item_id=cur.fetchone()['id'];conn.commit()
        return jsonify(ok=True,item_id=item_id,url=f'/brands/{brand_id}/work/{item_id}'),201
    finally: conn.close()

@marketing.route('/brands/<int:brand_id>/work/<int:item_id>')
def work_detail(brand_id,item_id):
    conn=models.db()
    try:
        cur=conn.cursor();cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s',(item_id,brand_id));item=cur.fetchone()
        if not item: abort(404)
        cur.execute('SELECT * FROM brands WHERE id=%s',(brand_id,));brand=cur.fetchone()
        item['brief']=_object(item['brief'])
        return render_template('marketing_work.html',brand=brand,item=item)
    finally: conn.close()

@marketing.route('/api/brands/<int:brand_id>/work-items/<int:item_id>/export')
def work_export(brand_id, item_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s', (item_id,brand_id))
        item = cur.fetchone()
        if not item: abort(404)
        response = make_response(json.dumps(dict(item), default=str, indent=2))
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Disposition'] = f'attachment; filename="marketing-draft-{item_id}.json"'
        return response
    finally: conn.close()

@marketing.route('/api/brands/<int:brand_id>/work-items/<int:item_id>',methods=['POST'])
def work_update(brand_id,item_id):
    payload=request.get_json(silent=True) or {}
    if not isinstance(payload,dict):return jsonify(ok=False,error='Work item must be an object'),400
    try:
        revision=int(payload.get('revision',0));action=payload.get('action','save')
        if action not in ('save','draft','ready','archive'): raise ValueError('Unsupported action')
        if action=='save':
            payload['brand_id']=brand_id
            validated=domain_module().validate_work_item(payload)
    except (ValueError,TypeError) as exc:return jsonify(ok=False,error=str(exc)),400
    conn=models.db()
    try:
        cur=conn.cursor();cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s FOR UPDATE',(item_id,brand_id));item=cur.fetchone()
        if not item:return jsonify(ok=False,error='Work item not found'),404
        if item['revision']!=revision:return jsonify(ok=False,error='Draft changed. Reload before saving.'),409
        if action=='draft':
            cur.execute("SELECT id FROM tasks WHERE type='marketing_studio_draft' AND params->>'item_id'=%s AND params->>'brand_id'=%s AND status IN ('queued','running')",(str(item_id),str(brand_id)))
            existing=cur.fetchone()
            if existing:return jsonify(ok=True,task_id=existing['id'])
            cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('marketing_studio_draft','queued',%s,'dashboard_v2') RETURNING id",(json.dumps({'brand_id':brand_id,'item_id':item_id,'revision':revision}),))
            task_id=cur.fetchone()['id'];cur.execute('UPDATE marketing_work_items SET task_id=%s WHERE id=%s AND brand_id=%s',(task_id,item_id,brand_id));conn.commit();return jsonify(ok=True,task_id=task_id)
        if action=='save':
            cur.execute("UPDATE marketing_work_items SET title=%s,brief=%s,body=%s,planned_at=%s,state='draft',revision=revision+1,updated_at=now() WHERE id=%s AND brand_id=%s",(validated['title'],json.dumps(validated.get('brief',{})),validated.get('body',''),validated.get('planned_at'),item_id,brand_id))
        else:
            if action=='ready' and (not str(item.get('body') or '').strip() or _object(item.get('brief')).get('needs_input')):return jsonify(ok=False,error='Complete required inputs and review the draft first'),400
            cur.execute('UPDATE marketing_work_items SET state=%s,revision=revision+1,updated_at=now() WHERE id=%s AND brand_id=%s',('ready' if action=='ready' else 'archived',item_id,brand_id))
        conn.commit();return jsonify(ok=True)
    finally:conn.close()

CHANNEL_GUIDES = {
 'instagram': ('https://help.instagram.com/502981923235522', 'Create or select a brand account, switch to a professional account, set identity and links, then authorize the provider separately.'),
 'youtube': ('https://support.google.com/youtube/answer/1646861', 'Create a brand channel, customize its title, handle, description, avatar, banner and website links in YouTube Studio.'),
 'linkedin': ('https://www.linkedin.com/help/linkedin/answer/a543852', 'Create or select the organization page, confirm administrator ownership, and complete identity and service information.'),
 'facebook': ('https://www.facebook.com/business/tools/facebook-pages', 'Create or select the brand Page, confirm administrator access and complete its profile.'),
 'x': ('https://help.x.com/en/managing-your-account/how-to-customize-your-profile', 'Create or select the brand account and complete its handle, bio, images and website.'),
 'email': ('https://help.brevo.com/hc/en-us/categories/360000229110', 'Choose the brand-owned sender/provider, verify the sending domain, configure consent, unsubscribe, suppression and quotas.'),
 'blog': ('https://ghost.org/docs/', 'Choose the owned CMS destination, verify domain ownership, scoped access, private preview and recovery.'),
 'help': ('https://ghost.org/docs/', 'Select the help destination, verify product behavior, navigation, search, scoped publication and rollback.'),
}
CHANNEL_CHECKS = ('account_owned','identity_set','assets_uploaded','links_set','access_granted','first_content_reviewed')

_IDENTITY_SLOTS = ('avatar', 'banner')
_IDENTITY_HASH = re.compile(r'^[0-9a-f]{64}$')
_IDENTITY_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_IDENTITY_MIMES = {'image/png', 'image/jpeg'}


def _csv_safe(value):
    if isinstance(value, str) and value.lstrip()[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value


def _identity_json(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _identity_ref(value):
    if not isinstance(value, dict) or set(value) != {'work_id', 'source_revision', 'filename', 'sha256'}:
        return None
    if (type(value['work_id']) is not int or value['work_id'] <= 0
            or type(value['source_revision']) is not int or value['source_revision'] <= 0
            or type(value['filename']) is not str or not _IDENTITY_NAME.fullmatch(value['filename'])
            or type(value['sha256']) is not str or not _IDENTITY_HASH.fullmatch(value['sha256'])):
        return None
    return dict(value)


@marketing.route('/brands/<int:brand_id>/channels/<channel>')
def channel_setup(brand_id,channel):
    if channel not in CHANNEL_GUIDES: abort(404)
    conn=models.db()
    try:
        cur=conn.cursor();cur.execute('SELECT id,name FROM brands WHERE id=%s',(brand_id,));brand=cur.fetchone()
        if not brand:abort(404)
        cur.execute('SELECT * FROM marketing_channels WHERE brand_id=%s AND channel=%s',(brand_id,channel));saved=cur.fetchone() or {}
        cur.execute('SELECT profile FROM marketing_brand_profiles WHERE brand_id=%s',(brand_id,));profile=(cur.fetchone() or {}).get('profile',{})
        cur.execute("SELECT id,brand_id,kind,channel,title,body,brief,state,revision,planned_at,updated_at FROM marketing_work_items WHERE brand_id=%s AND state<>'archived' ORDER BY updated_at DESC,id DESC LIMIT 100", (brand_id,))
        work_items = []
        for item in cur.fetchall():
            brief = _object(item.get('brief'))
            media = brief.get('media') if isinstance(brief.get('media'), dict) else {}
            if item.get('channel') == channel or media:
                item['brief'] = brief
                work_items.append(item)
        work_items = work_items[:30]
        setup_drafts = [item for item in work_items if item.get('channel') == channel and item.get('kind') == 'channel_setup'][:3]
        suggested = _object(setup_drafts[0].get('brief')) if setup_drafts else {}
        first_posts = [item for item in work_items if item.get('channel') == channel and item.get('kind') in ('social_post', 'video_brief')][:3]
        return render_template('marketing_channel.html',brand=brand,channel=channel,saved=saved,profile=profile,guide=CHANNEL_GUIDES[channel],checks=CHANNEL_CHECKS,work_items=work_items,first_posts=first_posts,suggested_identity={'display_name': suggested.get('suggested_display_name',''), 'bio': suggested.get('suggested_bio','')},identity_assets=_identity_json(saved.get('identity_assets')),identity_candidates=channel_identity.candidates(work_items, brand_id))
    finally:conn.close()

@marketing.route('/api/brands/<int:brand_id>/channels/<channel>',methods=['POST'])
def channel_save(brand_id,channel):
    if channel not in CHANNEL_GUIDES:abort(404)
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict):return jsonify(ok=False,error='Channel details must be an object'),400
    try:
        revision=payload.get('revision')
        if type(revision) is not int or revision < 0: raise ValueError('Revision required')
        values={key:payload.get(key,'') for key in ('profile_url','handle','display_name','bio')}
        if any(type(value) is not str for value in values.values()): raise ValueError('Channel details must be text')
        if any((ord(char) < 32 or ord(char) == 127) for key, value in values.items() if key != 'bio' for char in value): raise ValueError('Channel details contain invalid control characters')
        if any((ord(char) < 32 and char not in '\r\n\t') or ord(char) == 127 for char in values['bio']): raise ValueError('Bio contains invalid control characters')
        values={key:value.strip() for key,value in values.items()}
        if any(len(v)>2000 for v in values.values()):raise ValueError('Channel details are too long')
        domain_module().validate_profile({'positioning':values['bio'],'audience':values['handle'],'offer':values['display_name']})
        if values['profile_url']:
            parsed=urlsplit(values['profile_url'])
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:raise ValueError('Use a public HTTPS profile URL without credentials or tracking parameters')
            allowed={'instagram':('instagram.com',),'youtube':('youtube.com',),'linkedin':('linkedin.com',),'facebook':('facebook.com',),'x':('x.com','twitter.com')}.get(channel)
            if allowed and not any(parsed.hostname==host or parsed.hostname.endswith('.'+host) for host in allowed):raise ValueError('Profile URL does not match the channel')
        checked=payload.get('setup_checks',{})
        if not isinstance(checked,dict) or set(checked)-set(CHANNEL_CHECKS) or any(type(v)!=bool for v in checked.values()):raise ValueError('Invalid setup checklist')
        state='configured' if checked and all(checked.get(k) for k in CHANNEL_CHECKS) else 'owner_setup'
        has_assets = 'identity_assets' in payload
        identity_assets = payload.get('identity_assets') if has_assets else None
        if has_assets:
            if not isinstance(identity_assets, dict) or any(slot not in _IDENTITY_SLOTS for slot in identity_assets):
                raise ValueError('Invalid identity assets')
            identity_assets = {slot: _identity_ref(identity_assets[slot]) for slot in identity_assets}
            if any(identity_assets[slot] is None for slot in identity_assets): raise ValueError('Invalid identity asset reference')
    except (ValueError,TypeError) as exc:return jsonify(ok=False,error=str(exc)),400
    conn=models.db()
    try:
        cur=conn.cursor()
        try:
            cur.execute("SET LOCAL lock_timeout='2s'");cur.execute('SELECT id FROM brands WHERE id=%s FOR UPDATE',(brand_id,))
        except Exception:
            conn.rollback()
            return jsonify(ok=False,error='Brand setup is busy; retry'),409
        if not cur.fetchone():return jsonify(ok=False,error='Brand not found'),404
        cur.execute('SELECT revision,identity_assets FROM marketing_channels WHERE brand_id=%s AND channel=%s',(brand_id,channel));row=cur.fetchone()
        if (row['revision'] if row else 0)!=revision:return jsonify(ok=False,error='Setup changed. Reload before saving.'),409
        if has_assets:
            previous = _identity_json(row.get('identity_assets') if row else {})
            sources = {}
            try:
                for ref in sorted(identity_assets.values(), key=lambda value: value['work_id']):
                    if ref['work_id'] in sources:
                        continue
                    cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s FOR SHARE', (ref['work_id'], brand_id))
                    source = cur.fetchone()
                    if not source:
                        raise ValueError('source unavailable')
                    sources[ref['work_id']] = source
            except Exception:
                conn.rollback()
                return jsonify(ok=False,error='Identity asset source is busy or unavailable'),409
            for slot, ref in identity_assets.items():
                source = sources[ref['work_id']]
                prior = previous.get(slot)
                if not (isinstance(prior, dict) and prior == ref):
                    if source.get('revision') != ref['source_revision']:
                        return jsonify(ok=False,error='New identity asset must use the current source revision'),409
                try:
                    channel_identity.resolve_reference(dict(source), brand_id, ref)
                except (OSError, ValueError, TypeError):
                    return jsonify(ok=False,error='Identity asset is stale or unavailable'),409
        else:
            identity_assets = _identity_json(row.get('identity_assets') if row else {})
        cur.execute('''INSERT INTO marketing_channels(brand_id,channel,profile_url,handle,display_name,bio,setup_checks,connection_state,identity_assets)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(brand_id,channel) DO UPDATE SET
            profile_url=EXCLUDED.profile_url,handle=EXCLUDED.handle,display_name=EXCLUDED.display_name,bio=EXCLUDED.bio,
            setup_checks=EXCLUDED.setup_checks,connection_state=EXCLUDED.connection_state,
            identity_assets=EXCLUDED.identity_assets,revision=marketing_channels.revision+1,updated_at=now() RETURNING revision''',(brand_id,channel,values['profile_url'],values['handle'],values['display_name'],values['bio'],json.dumps(checked),state,json.dumps(identity_assets)))
        rev=cur.fetchone()['revision']
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('marketing_channel_setup','done',%s,'dashboard_v2') RETURNING id",(json.dumps({'brand_id':brand_id,'channel':channel,'revision':rev,'identity_assets':identity_assets}),))
        task_id = (cur.fetchone() or {}).get('id')
        conn.commit();return jsonify(ok=True,revision=rev,state=state,verified=False,identity_assets=identity_assets,task_id=task_id)
    finally:conn.close()

@marketing.route('/brands/<int:brand_id>/assets')
def brand_assets(brand_id):
    conn=models.db()
    try:
        cur=conn.cursor();cur.execute('SELECT id,name FROM brands WHERE id=%s',(brand_id,));brand=cur.fetchone()
        if not brand:abort(404)
        cur.execute('SELECT id,metadata,created_at FROM content_assets WHERE brand_id=%s ORDER BY id DESC LIMIT 100',(brand_id,));assets=cur.fetchall()
        return render_template('marketing_assets.html',brand=brand,assets=assets)
    finally:conn.close()


@marketing.route('/brands/<int:brand_id>/channels/<channel>/identity/<slot>')
def channel_identity_image(brand_id, channel, slot):
    if channel not in CHANNEL_GUIDES or slot not in _IDENTITY_SLOTS:
        abort(404)
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT identity_assets FROM marketing_channels WHERE brand_id=%s AND channel=%s', (brand_id, channel))
        saved = cur.fetchone() or {}
        ref = _identity_json(saved.get('identity_assets')).get(slot)
        if not _identity_ref(ref):
            abort(404)
        cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s', (ref['work_id'], brand_id))
        source = cur.fetchone()
        if not source:
            abort(404)
        try:
            resolved = channel_identity.resolve_reference(dict(source), brand_id, ref)
        except (OSError, ValueError, TypeError):
            abort(404)
        response = make_response(resolved['image_bytes'])
        response.headers['Content-Type'] = resolved['metadata']['mime']
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    finally:
        conn.close()


@marketing.route('/brands/<int:brand_id>/channels/<channel>/launch-kit')
def channel_launch_kit(brand_id, channel):
    if channel not in CHANNEL_GUIDES:
        abort(404)
    conn = models.db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM marketing_channels WHERE brand_id=%s AND channel=%s', (brand_id, channel))
        saved = cur.fetchone() or {}
        cur.execute('SELECT id,name FROM brands WHERE id=%s', (brand_id,))
        brand = cur.fetchone()
        if not brand:
            abort(404)
        cur.execute("SELECT id,brand_id,kind,channel,title,body,brief,state,revision,planned_at FROM marketing_work_items WHERE brand_id=%s AND state<>'archived' AND channel=%s AND kind IN ('social_post','video_brief','channel_setup') ORDER BY planned_at NULLS LAST,id DESC LIMIT 20", (brand_id, channel))
        work = cur.fetchall()
        raw_refs = saved.get('identity_assets')
        if isinstance(raw_refs, dict):
            refs = raw_refs
        elif isinstance(raw_refs, str):
            try:
                refs = json.loads(raw_refs)
            except (TypeError, ValueError):
                refs = None
        else:
            refs = {} if raw_refs in (None, '') else None
        if not isinstance(refs, dict):
            return jsonify(ok=False, error='Saved identity asset is stale or unavailable'), 409
        if set(refs) - set(_IDENTITY_SLOTS):
            return jsonify(ok=False, error='Saved identity asset is stale or unavailable'), 409
        files = []
        for slot in _IDENTITY_SLOTS:
            raw_ref = refs.get(slot)
            if raw_ref is None:
                continue
            ref = _identity_ref(raw_ref)
            if not ref:
                return jsonify(ok=False, error='Saved identity asset is stale or unavailable'), 409
            cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s', (ref['work_id'], brand_id))
            source = cur.fetchone()
            if not source:
                return jsonify(ok=False, error='Saved identity asset is stale or unavailable'), 409
            try:
                resolved = channel_identity.resolve_reference(dict(source), brand_id, ref)
                extension = 'png' if resolved['metadata']['mime'] == 'image/png' else 'jpg'
                files.append((f'{slot}.{extension}', resolved['image_bytes']))
            except (OSError, ValueError, TypeError):
                return jsonify(ok=False, error='Saved identity asset is stale or unavailable'), 409
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('README.txt', 'Reviewable channel launch kit. Steps: confirm account ownership, apply the saved identity, check links in a signed-out browser, review each draft and planned date, then complete provider authorization separately. Owner progress only. Provider connection remains unverified. This archive does not approve API access, publishing, or publication. Official guide: ' + CHANNEL_GUIDES[channel][0] + '\n')
            archive.writestr('identity.json', json.dumps({'brand_id': brand_id, 'channel': channel, 'display_name': saved.get('display_name',''), 'handle': saved.get('handle',''), 'bio': saved.get('bio',''), 'profile_url': saved.get('profile_url',''), 'setup_checks': saved.get('setup_checks',{}), 'identity_assets': refs, 'revision': saved.get('revision', 0), 'provider_verified': False}, sort_keys=True, default=str))
            for name, data in files:
                archive.writestr(name, data)
            rows = [['work_id', 'title', 'kind', 'channel', 'state', 'revision', 'planned_at', 'body']]
            for item in work:
                body_value = item.get('body')
                if not isinstance(body_value, str) or len(body_value) > 50000:
                    return jsonify(ok=False, error='Launch kit draft body is unavailable'), 409
                body = body_value
                planned = item.get('planned_at')
                planned_text = planned.astimezone(timezone.utc).isoformat() if hasattr(planned, 'astimezone') else (str(planned) if planned else '')
                values = [item.get('id',''), item.get('title',''), item.get('kind',''), item.get('channel',''), item.get('state',''), item.get('revision',''), planned_text, body]
                rows.append([_csv_safe(value) for value in values])
            csv_buffer = io.StringIO(newline='')
            csv.writer(csv_buffer, lineterminator='\n').writerows(rows)
            archive.writestr('drafts.csv', csv_buffer.getvalue())
        output.seek(0)
        response = send_file(output, mimetype='application/zip', as_attachment=True, download_name=f'channel-launch-kit-{brand_id}-{channel}.zip')
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    finally:
        conn.close()

@marketing.route('/api/brands/<int:brand_id>/work-items/<int:item_id>/media',methods=['POST'])
def media_generate(brand_id,item_id):
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict):return jsonify(ok=False,error='Media request must be an object'),400
    try:revision=int(payload.get('revision',0))
    except (TypeError,ValueError):return jsonify(ok=False,error='Revision required'),400
    conn=models.db()
    try:
        cur=conn.cursor();cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s FOR UPDATE',(item_id,brand_id));item=cur.fetchone()
        if not item:return jsonify(ok=False,error='Work item not found'),404
        if item['revision']!=revision:return jsonify(ok=False,error='Draft changed. Reload before generating media.'),409
        if not item.get('body') or _object(item.get('brief')).get('needs_input'):return jsonify(ok=False,error='Complete the brief and prepare a draft first'),400
        cur.execute("SELECT id FROM tasks WHERE type='marketing_media_generate' AND params->>'item_id'=%s AND params->>'brand_id'=%s AND status IN ('queued','running')",(str(item_id),str(brand_id)));existing=cur.fetchone()
        if existing:return jsonify(ok=True,task_id=existing['id'])
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('marketing_media_generate','queued',%s,'dashboard_v2') RETURNING id",(json.dumps({'brand_id':brand_id,'item_id':item_id,'revision':revision}),));tid=cur.fetchone()['id']
        cur.execute('UPDATE marketing_work_items SET task_id=%s WHERE id=%s AND brand_id=%s',(tid,item_id,brand_id));conn.commit();return jsonify(ok=True,task_id=tid)
    finally:conn.close()

@marketing.route('/brands/<int:brand_id>/work/<int:item_id>/media/<path:filename>')
def media_file(brand_id,item_id,filename):
    from pathlib import Path
    from flask import send_file
    conn=models.db()
    try:
        cur=conn.cursor();cur.execute('SELECT brief FROM marketing_work_items WHERE id=%s AND brand_id=%s',(item_id,brand_id));row=cur.fetchone()
        if not row:abort(404)
        manifest=_object(row['brief']).get('media') or {}
        roots=Path('/home/agency/.local/share/agency-marketing/brands')/str(brand_id)
        outputs=manifest.get('outputs',[])
        candidates=[Path(o['path']) for o in outputs if isinstance(o,dict) and o.get('path') and Path(o['path']).name==filename]
        if len(candidates)!=1:abort(404)
        file=candidates[0].resolve()
        try:file.relative_to(roots.resolve())
        except ValueError:abort(404)
        if file.suffix.lower() not in ('.png','.jpg','.jpeg','.mp4','.zip','.json','.txt','.pdf'):abort(404)
        response=send_file(file,as_attachment=request.args.get('download')=='1')
        response.headers['X-Content-Type-Options']='nosniff';response.headers['Cache-Control']='private, no-store'
        return response
    finally:conn.close()

@marketing.route('/api/brands/<int:brand_id>/work-items/<int:item_id>/ui-asset',methods=['POST'])
def upload_ui_asset(brand_id,item_id):
    import io,hashlib
    from pathlib import Path
    from PIL import Image,ImageOps,UnidentifiedImageError
    if request.form.get('rights_confirmed')!='true':return jsonify(ok=False,error='Confirm ownership and removal of private user information'),400
    try:revision=int(request.form.get('revision',0))
    except (ValueError,TypeError):return jsonify(ok=False,error='Revision required'),400
    file=request.files.get('file')
    if not file:return jsonify(ok=False,error='Upload a product screenshot'),400
    try:
        data=file.read(12*1024*1024+1)
        if len(data)>12*1024*1024:raise ValueError('Image exceeds 12 MB')
        with Image.open(io.BytesIO(data)) as image:
            if image.width*image.height>24000000:raise ValueError('Image exceeds pixel limit')
            image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((2400,2400));buffer=io.BytesIO();image.save(buffer,format='PNG');clean=buffer.getvalue()
    except (ValueError,UnidentifiedImageError,OSError):return jsonify(ok=False,error='Upload a valid bounded PNG/JPEG screenshot'),400
    conn=models.db()
    try:
        cur=conn.cursor();cur.execute('SELECT * FROM marketing_work_items WHERE id=%s AND brand_id=%s FOR UPDATE',(item_id,brand_id));item=cur.fetchone()
        if not item:return jsonify(ok=False,error='Work item not found'),404
        if item['revision']!=revision:return jsonify(ok=False,error='Draft changed. Reload before uploading.'),409
        base=Path('/home/agency/.local/share/agency-marketing/brands')
        root=base/str(brand_id)/'inputs'/str(item_id)
        filename=hashlib.sha256(clean).hexdigest()+'.png'
        fds=[]
        try:
            directory_fd,fds=_upload_directory_fd(base,brand_id,item_id)
            _store_upload_bytes(directory_fd,filename,clean)
        except (OSError,ValueError):
            return jsonify(ok=False,error='Upload destination is not a safe brand-owned directory or file'),409
        finally:
            for fd in reversed(fds):os.close(fd)
        path=root/filename
        brief=_object(item['brief']);brief['project_ui_asset']=str(path);brief['ui_asset_provenance']='Owner-uploaded product screenshot, metadata stripped; owner confirms no private user information.'
        brief.pop('media',None);brief['needs_input']=[x for x in brief.get('needs_input',[]) if x!='project_ui_asset']
        cur.execute("UPDATE marketing_work_items SET brief=%s,state='draft',revision=revision+1,updated_at=now() WHERE id=%s AND brand_id=%s",(json.dumps(brief),item_id,brand_id));conn.commit();return jsonify(ok=True)
    finally:conn.close()

@marketing.route('/calendar')
def marketing_calendar():
    raw_brand = request.args.get('brand_id')
    if raw_brand is None or raw_brand == '':
        brand_filter = None
    elif raw_brand.isascii() and raw_brand.isdigit() and len(raw_brand) <= 10 and 0 < int(raw_brand) <= 2147483647:
        brand_filter = int(raw_brand)
    else:
        return jsonify(ok=False, error='brand_id must be a positive integer'), 400
    conn=models.db()
    try:
        cur=conn.cursor()
        cur.execute('SELECT id,name FROM brands ORDER BY name,id LIMIT 250')
        brands = cur.fetchall()
        plan_where = "WHERE m.state<>'archived' AND m.planned_at IS NOT NULL"
        plan_args = []
        if brand_filter is not None:
            plan_where += ' AND m.brand_id=%s'; plan_args.append(brand_filter)
        cur.execute('''SELECT m.id,m.brand_id,b.name AS brand_name,m.title,m.kind,m.channel,m.state AS status,m.planned_at AS planned_at,
            '/brands/'||m.brand_id||'/work/'||m.id AS url FROM marketing_work_items m JOIN brands b ON b.id=m.brand_id
            ''' + plan_where + '''
            UNION ALL SELECT c.id,c.brand_id,b.name,c.title,'editorial','blog',c.status,c.planned_date::timestamptz,
            '/content/calendar?brand_id='||c.brand_id FROM content_calendar c JOIN brands b ON b.id=c.brand_id
            WHERE c.status<>'cancelled' ''' + (' AND c.brand_id=%s' if brand_filter is not None else '') + '''
            ORDER BY planned_at,brand_name,title LIMIT 250''', tuple(plan_args + plan_args))
        plans = cur.fetchall()
        run_where = 'WHERE 1=1'
        run_args = []
        if brand_filter is not None:
            run_where += ' AND r.brand_id=%s'; run_args.append(brand_filter)
        cur.execute('''SELECT r.id,r.brand_id,b.name AS brand_name,r.item_id,r.revision,r.send_at,r.state,
            wi.title AS title
            FROM marketing_campaign_runs r JOIN brands b ON b.id=r.brand_id
            JOIN marketing_work_items wi ON wi.id=r.item_id AND wi.brand_id=r.brand_id
            ''' + run_where + ''' ORDER BY r.send_at,r.id LIMIT 250''', tuple(run_args))
        runs = cur.fetchall()
        for row in plans:
            if row.get('planned_at'): row['planned_at'] = row['planned_at'].astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')
        for row in runs:
            if row.get('send_at'): row['send_at'] = row['send_at'].astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        return render_template('marketing_calendar.html', plans=plans, runs=runs, brands=brands, brand_filter=brand_filter)
    finally:conn.close()

@marketing.route('/api/brands/<int:brand_id>/measurement-schedule',methods=['POST'])
def measurement_schedule(brand_id):
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict) or type(payload.get('enabled'))!=bool:return jsonify(ok=False,error='Explicit enabled boolean required'),400
    conn=models.db()
    try:
        cur=conn.cursor();cur.execute('SELECT id FROM brands WHERE id=%s FOR UPDATE',(brand_id,))
        if not cur.fetchone():return jsonify(ok=False,error='Brand not found'),404
        cur.execute('INSERT INTO marketing_measurement_schedules(brand_id,enabled) VALUES (%s,%s) ON CONFLICT(brand_id) DO UPDATE SET enabled=EXCLUDED.enabled,updated_at=now()',(brand_id,payload['enabled']))
        conn.commit();return jsonify(ok=True,enabled=payload['enabled'],schedule='Daily 06:30 UTC collection only')
    finally:conn.close()
