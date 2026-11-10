"""Small, reviewable text/outline revisions for content items.

This module deliberately does not compose, publish, or call the worker.  It is
the dashboard's revision boundary: the browser previews a complete candidate,
then saves that exact candidate with an audit snapshot.
"""
import copy
import hashlib
import html
import json
import re
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

import models
from script_paths import ensure_agency_scripts

content_text_edits = Blueprint("content_text_edits", __name__)
_META_LIMITS = {"meta_title": 2000, "meta_description": 2000, "custom_excerpt": 300,
                "target_keyword": 200, "help_slug": 150}
_PROTECTED = {"image_slot", "editorial_visual", "table", "chart", "callout"}
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TEXT_KEYS = ("markdown", "heading", "brief", "answer", "question", "alt", "prompt",
              "caption", "answer_pointer", "label", "stat")
_LIST_KEYS = ("points", "steps", "sources", "fact_ids")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _revision(item, blocks=None, structured=None, title=None):
    from content_visuals import revision
    return revision(item)


def _error(exc):
    code = 404 if isinstance(exc, LookupError) else 409 if isinstance(exc, PermissionError) else 400
    return jsonify(ok=False, error=str(exc)), code


def _get_item(cur, item_id):
    cur.execute("SELECT ci.*, p.lifecycle FROM content_items ci JOIN brands b ON b.id=ci.brand_id "
                "JOIN projects p ON p.id=b.project_id WHERE ci.id=%s FOR UPDATE OF ci", (item_id,))
    item = cur.fetchone()
    if not item:
        raise LookupError("Content item not found")
    if item.get("lifecycle") != "active":
        raise PermissionError("Only active engagements can be edited")
    if item.get("status") not in ("draft", "outline"):
        raise PermissionError("Only draft or outline content can be edited")
    cur.execute("SELECT id,type,status FROM tasks WHERE status IN ('queued','running') "
                "AND params->>'content_item_id'=%s AND type IN "
                "('content_compose','publish_content','content_text_edit') ORDER BY id DESC LIMIT 1", (str(item_id),))
    active = cur.fetchone()
    if active:
        raise PermissionError("An active content task is already working on this item")
    if not isinstance(item.get("structured"), dict):
        raise ValueError("Content metadata must be an object")
    if item["status"] == "outline" and item.get("content_blocks"):
        raise PermissionError("This outline has composed checkpoints. Resume composition or use regeneration.")
    structured = item["structured"]
    blocks = item.get("content_blocks") if item.get("status") == "draft" else structured.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("Content blocks must be an array")
    return item, blocks, structured


def _metadata(structured):
    return {key: structured[key] for key in _META_LIMITS if key in structured}


def _payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Edit must be a JSON object")
    allowed = {"revision", "title", "blocks", "metadata", "review_note", "remove_fact_ids", "preview_digest"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("Unknown edit fields: " + ", ".join(sorted(unknown)))
    if "title" in payload and (not isinstance(payload["title"], str) or not payload["title"].strip() or len(payload["title"]) > 240):
        raise ValueError("title must be a non-empty string of at most 240 characters")
    if "review_note" in payload and (not isinstance(payload["review_note"], str) or len(payload["review_note"]) > 2000):
        raise ValueError("review_note must be a string of at most 2000 characters")
    if not isinstance(payload.get("review_note"), str) or not payload["review_note"].strip():
        raise ValueError("Describe what changed in review_note")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict) or set(metadata) - set(_META_LIMITS):
        raise ValueError("metadata contains a non-editable field")
    for key, limit in _META_LIMITS.items():
        if key in metadata and (not isinstance(metadata[key], str) or len(metadata[key]) > limit):
            raise ValueError(key + " must be a string within its length limit")
        if key == "help_slug" and key in metadata and metadata[key] and not _SLUG.fullmatch(metadata[key]):
            raise ValueError("help_slug must use lowercase hyphen-separated words")
    removals = payload.get("remove_fact_ids", [])
    if not isinstance(removals, list) or any(not isinstance(x, (str, int)) for x in removals):
        raise ValueError("remove_fact_ids must be an array of IDs")
    return payload


def _validate_shape(blocks):
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 60:
        raise ValueError("blocks must contain 1 to 60 objects")
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ValueError("block %s must be an object with a type" % index)
        if block["type"] not in _PROTECTED:
            allowed = set(_TEXT_KEYS) | set(_LIST_KEYS) | {"type", "keyword_target"}
            if set(block) - allowed:
                raise ValueError("Unexpected fields in text block %s" % index)
        if "keyword_target" in block and type(block["keyword_target"]) is not bool:
            raise ValueError("keyword_target must be boolean")
        for key in _TEXT_KEYS:
            if key in block and not isinstance(block[key], str):
                raise ValueError("block %s.%s must be a string" % (index, key))
        for key in _LIST_KEYS:
            if key in block and (not isinstance(block[key], list) or any(not isinstance(v, str) for v in block[key])):
                raise ValueError("block %s.%s must be a list of strings" % (index, key))


def _outline_validate(blocks, facts):
    _validate_shape(blocks)
    allowed = {"intro", "prose", "heading", "key_takeaways", "steps", "faq", "image_slot", "table", "chart", "callout"}
    if any(b["type"] not in allowed for b in blocks):
        raise ValueError("Unknown outline block type")
    if len(blocks) > 18:
        raise ValueError("outlines may contain at most 18 blocks")
    known = {str(f.get("id")) for f in facts if isinstance(f, dict) and f.get("id") is not None}
    for i, block in enumerate(blocks):
        typ = block.get("type")
        if not (block.get("brief") or "").strip():
            raise ValueError("block %s needs a brief" % i)
        refs = block.get("fact_ids", [])
        if not isinstance(refs, list) or any(str(x) not in known for x in refs):
            raise ValueError("block %s contains an unknown fact reference" % i)
        if typ == "faq" and not (block.get("answer_pointer") or "").strip():
            raise ValueError("FAQ block needs answer_pointer")
        if typ == "image_slot" and (not (block.get("alt") or "").strip() or not (block.get("prompt") or "").strip()):
            raise ValueError("image_slot needs alt and prompt")
        if typ == "chart" and block.get("chart_type") not in ("bar", "line", "pie"):
            raise ValueError("chart_type must be bar, line, or pie")
        if typ in ("table", "chart", "callout") and not refs:
            raise ValueError("%s requires at least one fact reference" % typ)
    if not any(b.get("type") == "intro" for b in blocks) or not any(b.get("type") == "prose" and b.get("keyword_target") is True for b in blocks):
        raise ValueError("outline needs intro and keyword-target prose blocks")


def _check_facts(blocks, structured, removals):
    facts = structured.get("facts") or []
    ids = {str(f.get("id")) for f in facts if isinstance(f, dict) and f.get("id") is not None}
    removing = {str(x) for x in removals}
    if not removing <= ids:
        raise ValueError("remove_fact_ids contains an unknown fact")
    refs = {str(x) for b in blocks for x in (b.get("fact_ids") or []) if isinstance(b, dict)}
    if refs & removing:
        raise ValueError("A fact cannot be removed while a block still references it")
    if not refs <= ids:
        raise ValueError("Blocks contain an unknown fact reference")
    return [f for f in facts if str(f.get("id")) not in removing]


def _candidate(item, old_blocks, structured, payload):
    payload = _payload(payload)
    if payload.get("revision") != _revision(item, old_blocks, structured):
        raise PermissionError("Content changed. Reload and preview again.")
    if item["status"] == "outline" and "target_keyword" in payload.get("metadata", {}) and not payload["metadata"]["target_keyword"].strip():
        raise ValueError("Outline target_keyword cannot be empty")
    blocks = copy.deepcopy(payload.get("blocks", old_blocks))
    _validate_shape(blocks)
    old_protected = [b for b in old_blocks if isinstance(b, dict) and b.get("type") in _PROTECTED]
    new_protected = [b for b in blocks if b.get("type") in _PROTECTED]
    if item["status"] == "draft" and old_protected != new_protected:
        raise ValueError("Text editing cannot change reviewed assets, tables, charts, or callouts")
    if item.get("status") == "outline":
        def resolved_media(values):
            return [b for b in values if b.get("type") in {"image_slot", "editorial_visual"} and any(b.get(k) for k in ("url", "image_url", "asset", "asset_metadata", "reviewed"))]
        if resolved_media(old_blocks) != resolved_media(blocks):
            raise ValueError("Use the asset editor to change resolved media")
        checkpoints = structured.get("compose_checkpoint") or structured.get("compose_checkpoints") or structured.get("checkpoint")
        if checkpoints:
            raise PermissionError("This outline has compose checkpoints. Use the regeneration workflow.")
        _outline_validate(blocks, structured.get("facts") or [])
        quality = {"findings": [], "blocking": False}
    else:
        ensure_agency_scripts()
        from content_quality import validate_content
        quality = validate_content(blocks, "draft")
        structural = [f for f in quality["findings"] if f["code"] not in {"unresolved_image", "missing_alt", "missing_image_brief"}]
        if structural:
            raise ValueError("Resolve content structure before saving: " + "; ".join(f["message"] for f in structural))
    facts = _check_facts(blocks, structured, payload.get("remove_fact_ids", []))
    new_structured = copy.deepcopy(structured)
    new_structured.update(payload.get("metadata") or {})
    new_structured["facts"] = facts
    candidate = {"title": payload.get("title", item.get("title")), "blocks": blocks,
                 "metadata": _metadata(new_structured), "structured": new_structured,
                 "review_note": payload.get("review_note", "")}
    if item.get("status") == "draft":
        quality = validate_content(blocks, "draft")
    return candidate, quality


def _assemble_plain(filled):
    """Render filled blocks to a readable body (pulls the article together for preview/ledger)."""
    parts = []
    sources = []
    for b in filled:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("intro", "heading"):
            text = b.get("markdown") or b.get("heading") or ""
            if text:
                parts.append((f"## {text}" if t == "heading" else text))
        elif t == "prose":
            if b.get("markdown"):
                parts.append(b["markdown"])
        elif t == "key_takeaways":
            pts = b.get("points") or []
            if pts:
                parts.append("**Key takeaways**\n" + "\n".join(f"- {p}" for p in pts))
        elif t == "steps":
            st = b.get("steps") or []
            if st:
                parts.append("\n".join(f"{i}. {s}" for i, s in enumerate(st, 1)))
        elif t == "table":
            columns, rows = b.get("columns") or [], b.get("rows") or []
            if columns and rows:
                parts.append(
                    "| " + " | ".join(str(v) for v in columns) + " |\n"
                    + "| " + " | ".join("---" for _ in columns) + " |\n"
                    + "\n".join("| " + " | ".join(str(v) for v in row) + " |" for row in rows)
                )
        elif t == "chart":
            series = b.get("data_series") or {}
            labels, values = series.get("labels") or [], series.get("values") or []
            if labels and len(labels) == len(values):
                title = b.get("title") or "Data"
                chart_lines = [f"**{title}** ({b.get('chart_type', 'chart')})"]
                chart_lines += [f"- {label}: {value}" for label, value in zip(labels, values)]
                parts.append("\n".join(chart_lines))
        elif t == "callout":
            if b.get("stat"):
                parts.append(f"> **{b.get('label','')}:** {b['stat']}")
        elif t == "faq":
            if b.get("answer"):
                parts.append(f"**{b.get('brief','Q')}**\n{b['answer']}")
        elif t == "image_slot" and b.get("alt"):
            image_url = b.get('image_url') or b.get('url')
            parts.append(f"![{b['alt']}]({image_url})" if image_url else f"_[Image planned: {b['alt']}]_")
            if b.get('caption'):
                parts.append(b['caption'])
        for url in b.get("sources") or []:
            if url and url not in sources:
                sources.append(url)
    if sources:
        parts += ["## Sources", "\n".join(f"{i}. {url}" for i, url in enumerate(sources, 1))]
    return "\n\n".join(parts).rstrip()

def _body(blocks, structured=None):
    base = _assemble_plain([b for b in blocks if b.get("type") != "editorial_visual"])
    visuals = [b for b in blocks if b.get("type") == "editorial_visual"]
    if visuals or (isinstance(structured, dict) and "visual_base_body" in structured):
        ensure_agency_scripts()
        import editorial_visuals
        if isinstance(structured, dict):
            structured["visual_base_body"] = base
        return base + "".join("\n\n" + editorial_visuals.visual_markdown(b) for b in visuals)
    return base


def _html_preview(candidate, status):
    if status == "draft":
        ensure_agency_scripts()
        from content_pipeline import render_content_blocks
        return render_content_blocks(candidate["blocks"], candidate["title"])
    return "<div>" + "".join("<p>" + html.escape(b.get("brief", "")) + "</p>" for b in candidate["blocks"]) + "</div>"


@content_text_edits.before_request
def _same_site():
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if (origin and urlsplit(origin).netloc != request.host) or request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(ok=False, error="Cross-site changes are not permitted"), 403


@content_text_edits.get("/content/<int:item_id>/edit-data")
def edit_data(item_id):
    conn = models.db()
    try:
        item, blocks, structured = _get_item(conn.cursor(), item_id)
        return jsonify(ok=True, revision=_revision(item, blocks, structured), status=item["status"], title=item.get("title"), blocks=blocks, metadata=_metadata(structured))
    except (ValueError, PermissionError, LookupError) as exc:
        return _error(exc)
    finally:
        conn.rollback(); conn.close()


def _preview(item_id, save=False):
    if request.content_length and request.content_length > 200000:
        return jsonify(ok=False, error="Edit payload exceeds 200 KB"), 413
    raw = request.stream.read(200001)
    if len(raw) > 200000:
        return jsonify(ok=False, error="Edit payload exceeds 200 KB"), 413
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return jsonify(ok=False, error="Edit must be a JSON object"), 400
    conn = models.db()
    try:
        cur = conn.cursor(); item, blocks, structured = _get_item(cur, item_id)
        candidate, quality = _candidate(item, blocks, structured, payload)
        digest = _digest(candidate)
        if not save:
            return jsonify(ok=True, preview_digest=digest, quality=quality, html=_html_preview(candidate, item["status"]), candidate=candidate)
        if payload.get("preview_digest") != digest:
            raise PermissionError("Preview this exact edit before saving it")
        before = {key: item.get(key) for key in ("title", "status", "content_blocks", "body", "structured", "approval_id", "publish_task_id")}
        new_structured = candidate["structured"]
        new_structured.pop("link_report", None)
        if item["status"] == "outline":
            new_structured.pop("quality_report", None)
            new_structured["blocks"] = candidate["blocks"]
            new_blocks, body = item.get("content_blocks") or [], item.get("body")
        else:
            new_blocks, body = candidate["blocks"], _body(candidate["blocks"], new_structured)
            new_structured.pop("link_report", None)
            new_structured["quality_report"] = quality
        if item["status"] == "draft":
            cur.execute("UPDATE content_items SET title=%s,content_blocks=%s,body=%s,structured=%s,approval_id=NULL,publish_task_id=NULL,updated_at=now() WHERE id=%s RETURNING updated_at", (candidate["title"], json.dumps(new_blocks), body, json.dumps(new_structured), item_id))
        else:
            cur.execute("UPDATE content_items SET title=%s,structured=%s,updated_at=now() WHERE id=%s RETURNING updated_at", (candidate["title"], json.dumps(new_structured), item_id))
        updated = cur.fetchone()["updated_at"]
        after = {**item, "title": candidate["title"], "content_blocks": new_blocks, "body": body, "structured": new_structured, "updated_at": updated}
        result = {"before": before, "changed_fields": [key for key in ("title", "blocks", "metadata", "review_note", "facts") if key in payload or (key == "facts" and payload.get("remove_fact_ids"))], "after_revision": _revision(after, new_blocks, new_structured), "publication": False, "review_note": payload["review_note"]}
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by,result_ref,started_at,finished_at) VALUES ('content_text_edit','done',%s,'dashboard-content-edit',%s,now(),now()) RETURNING id", (json.dumps({"content_item_id": item_id, "review_note": payload.get("review_note", "")}), json.dumps(result, default=str)))
        task_id = cur.fetchone()["id"]; conn.commit()
        return jsonify(ok=True, revision=result["after_revision"], task_id=task_id)
    except (ValueError, PermissionError, LookupError, TypeError) as exc:
        conn.rollback(); return _error(exc)
    finally:
        conn.close()


@content_text_edits.post("/content/<int:item_id>/edit/preview")
def preview_edit(item_id):
    return _preview(item_id, False)


@content_text_edits.post("/content/<int:item_id>/edit")
def save_edit(item_id):
    return _preview(item_id, True)
