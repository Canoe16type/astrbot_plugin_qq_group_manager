"""踢人记录 Web API。"""

from __future__ import annotations

import time

from astrbot.api.web import json_response, request

from . import record as kick_record


async def web_kick_history(host):
    """GET /mod/kick/history?group=&limit=50 踢人记录（不传群则全部群）。"""
    limit = max(1, min(200, request.query.get("limit", 50, type=int)))
    g = str(request.query.get("group", "") or "")
    host._bind_log_group(g)
    rows = kick_record.recent(host, g, limit)
    items = []
    for r in rows:
        items.append({
            "id": r.get("id"),
            "ts": r.get("ts"),
            "time_text": time.strftime("%m-%d %H:%M", time.localtime(float(r.get("ts") or 0))),
            "group": r.get("group") or "",
            "operator": r.get("operator") or "",
            "operator_name": r.get("operator_name") or "",
            "targets": r.get("targets") or [],
            "reason": r.get("reason") or "",
            "ok": bool(r.get("ok")),
            "err_code": r.get("err_code"),
            "err_msg": r.get("err_msg") or "",
        })
    return json_response({"items": items, "total": len(items)})
