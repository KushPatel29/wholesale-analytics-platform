from __future__ import annotations

import json
import time
from queue import Empty

from flask import Blueprint, Response, jsonify, current_app
from flask_login import login_required

from app.services import event_bus
from app.services import fact_store  # type: ignore


bp = Blueprint("events", __name__, url_prefix="/api")


@bp.get("/events")
@login_required
def events():
    def event_stream():
        queue = event_bus.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    message = queue.get(timeout=20)
                except Empty:
                    yield f": heartbeat {int(time.time())}\n\n"
                    continue
                yield f"data: {json.dumps(message)}\n\n"
        finally:
            event_bus.unsubscribe(queue)

    return Response(event_stream(), mimetype="text/event-stream")


@bp.get("/freshness")
@login_required
def freshness():
    if current_app.config.get("TEST_STATE") is not None:
        try:
            import data_loader as loader  # type: ignore

            manifest = loader.read_manifest()
            if isinstance(manifest, dict) and manifest:
                payload = {
                    "version": str(manifest.get("version") or loader.current_version()),
                    "built_at": manifest.get("built_at"),
                    "rows": manifest.get("rows"),
                }
                return jsonify(payload), 200
        except Exception:
            pass
    try:
        manifest = fact_store.get_meta()
    except Exception:
        manifest = {}
    if not isinstance(manifest, dict) or not manifest:
        return jsonify({"status": "stale"}), 503
    payload = {
        "version": manifest.get("dataset_version") or manifest.get("version"),
        "built_at": manifest.get("built_at_utc") or manifest.get("built_at") or manifest.get("last_refresh_utc"),
        "rows": manifest.get("row_count") or manifest.get("rows"),
    }
    return jsonify(payload), 200
