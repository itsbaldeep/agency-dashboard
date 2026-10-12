"""Project agnostic first party journey connection setup.

Only non secret connection references are stored here. Provider credentials are
owned by the project ledger and are never read by the dashboard.
"""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

import models


connections = Blueprint("marketing_connections", __name__)
_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_MAX_PATH = 500
_SAFE_FIELDS = ("base_url", "endpoint", "credential_ref", "credential_name")


def _safe_config(value):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in _SAFE_FIELDS if isinstance(value.get(key), str)}


def _digest(config):
    body = json.dumps(_safe_config(config), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _endpoint(value):
    if not isinstance(value, str) or not value.startswith("/") or "?" in value or "#" in value or "\\" in value:
        return None
    if ".." in Path(value).parts or len(value) > 300:
        return None
    return value


def _localhost(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{port}" if port else f"{parsed.scheme}://{host}"


def _credential_path(value, project):
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH or "\x00" in value:
        return None
    classification = project.get("classification")
    if classification == "core":
        root = Path("/home/agency/.config/agency")
    elif classification == "engagement":
        root = Path(str(project.get("local_path") or ""))
        if not root.is_absolute():
            return None
    else:
        return None
    try:
        # The dashboard container may not mount the credential directory.
        # Keep this check lexical and let the worker verify the actual file.
        root = Path(os.path.normpath(str(root)))
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.normpath(str(candidate)))
        candidate.relative_to(root)
        allowed = Path("/home/agency/.config/agency" if classification == "core" else "/home/agency/engagements")
        candidate.relative_to(allowed)
    except (OSError, RuntimeError, ValueError):
        return None
    return str(candidate)


def _brand_project(cur, brand_id, *, lock=False):
    suffix = " FOR UPDATE OF b" if lock else ""
    cur.execute("SELECT b.id,b.project_id,p.local_path,p.classification,p.lifecycle "
                "FROM brands b LEFT JOIN projects p ON p.id=b.project_id WHERE b.id=%s" + suffix,
                (brand_id,))
    return cur.fetchone()


def _load_config(cur, brand_id):
    cur.execute("SELECT value FROM brand_properties WHERE brand_id=%s AND property_type='activation_config'", (brand_id,))
    row = cur.fetchone()
    if not row:
        return {}
    try:
        return _safe_config(json.loads(row.get("value") or "{}"))
    except (TypeError, ValueError):
        return {}


def _validated(payload, project):
    if not isinstance(payload, dict):
        raise ValueError("Connection setup must be an object")
    base_url = _localhost(payload.get("base_url", ""))
    endpoint = _endpoint(payload.get("endpoint", ""))
    credential_ref = _credential_path(payload.get("credential_ref", ""), project)
    credential_name = payload.get("credential_name")
    if not base_url:
        raise ValueError("Base URL must be a localhost URL")
    if not endpoint:
        raise ValueError("Endpoint must be a bounded local path")
    if not credential_ref:
        raise ValueError("Credential reference must stay inside the project credential root")
    if not isinstance(credential_name, str) or not _NAME.fullmatch(credential_name):
        raise ValueError("Credential name must be an uppercase environment variable reference")
    return {"base_url": base_url, "endpoint": endpoint,
            "credential_ref": credential_ref, "credential_name": credential_name}


@connections.before_request
def protect_writes():
    if request.content_length and request.content_length > 100000:
        return jsonify(ok=False, error="Request too large"), 413
    if request.method == "POST" and request.headers.get("Origin") != request.host_url.rstrip("/"):
        return jsonify(ok=False, error="Cross-origin write rejected"), 403


@connections.route("/api/brands/<int:brand_id>/marketing-connection", methods=["GET"])
def get_connection(brand_id):
    conn = models.db()
    try:
        cur = conn.cursor()
        brand = _brand_project(cur, brand_id)
        if not brand:
            return jsonify(ok=False, error="Brand not found"), 404
        if brand.get("lifecycle") not in (None, "active"):
            return jsonify(ok=False, error="Project lifecycle is not active"), 409
        config = _load_config(cur, brand_id)
        return jsonify(ok=True, configured=bool(config), config=config,
                       digest=_digest(config))
    finally:
        conn.close()


@connections.route("/api/brands/<int:brand_id>/marketing-connection", methods=["POST"])
def save_connection(brand_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="Connection setup must be an object"), 400
    conn = models.db()
    try:
        cur = conn.cursor()
        brand = _brand_project(cur, brand_id, lock=True)
        if not brand:
            return jsonify(ok=False, error="Brand not found"), 404
        if brand.get("lifecycle") not in (None, "active"):
            return jsonify(ok=False, error="Project lifecycle is not active"), 409
        current = _load_config(cur, brand_id)
        if payload.get("digest") != _digest(current):
            return jsonify(ok=False, error="Connection setup changed. Reload before saving."), 409
        try:
            config = _validated(payload, brand)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        cur.execute("INSERT INTO brand_properties (brand_id,property_type,value,accessible) VALUES (%s,'activation_config',%s,false) "
                    "ON CONFLICT (brand_id,property_type) DO UPDATE SET value=EXCLUDED.value,accessible=false,created_at=now()",
                    (brand_id, json.dumps(config, sort_keys=True, separators=(",", ":"))))
        cur.execute("INSERT INTO tasks(type,status,params,triggered_by) VALUES ('marketing_connection_setup','done',%s,'dashboard') RETURNING id",
                    (json.dumps({"brand_id": brand_id, "config_digest": _digest(config),
                                 "refs": {"base_url": config["base_url"], "endpoint": config["endpoint"],
                                          "credential_ref": config["credential_ref"], "credential_name": config["credential_name"]}}, sort_keys=True),))
        task = cur.fetchone() or {}
        conn.commit()
        return jsonify(ok=True, config=config, digest=_digest(config), task_id=task.get("id")), 201
    except Exception:
        conn.rollback()
        return jsonify(ok=False, error="Connection setup could not be saved"), 500
    finally:
        conn.close()
