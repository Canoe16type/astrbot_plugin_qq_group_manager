"""统一记录 Web API。"""
from __future__ import annotations

import time

from astrbot.api.web import json_response, request

from .query import get as get_record, query as query_records


def _time_text(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "-"


def _item(row) -> dict:
    return {
        "id": row.id,
        "display_id": row.display_id,
        "kind": row.kind,
        "group": row.group,
        "ts": row.ts,
        "time_text": _time_text(row.ts),
        "target_id": row.target_id,
        "target_ids": list(row.target_ids),
        "target_name": row.target_name,
        "actor_id": row.actor_id,
        "actor_name": row.actor_name,
        "action": row.action,
        "result": row.result,
        "ok": row.ok,
        "reason": row.reason,
        "detail": dict(row.detail),
        "legacy": row.legacy,
    }


async def web_records(host):
    """GET /records?group=&kind=&target=&days=&limit=50"""
    group = str(request.query.get("group", "") or "")
    kind = str(request.query.get("kind", "") or "")
    target = str(request.query.get("target", "") or "")
    days = max(0, min(365, request.query.get("days", 0, type=int)))
    limit = max(1, min(50, request.query.get("limit", 20, type=int)))
    since = time.time() - days * 86400 if days else 0.0
    rows = query_records(
        host, group=group, kinds=[kind] if kind else None,
        target_id=target, since=since, limit=limit,
    )
    return json_response({
        "items": [_item(r) for r in rows],
        "total": len(rows),
        "coverage": "仅记录本插件执行的操作；其他管理员的撤回/禁言不在此列。",
    })


async def web_records_detail(host):
    """GET /records/detail?id=V12&group="""
    group = str(request.query.get("group", "") or "")
    row = get_record(host, str(request.query.get("id", "") or ""), group=group)
    if not row:
        return json_response({"error": "记录不存在"}, status_code=404)
    item = _item(row)
    item["content"] = ""
    # 撤回的原始文字、判定的原文只给面板详情，不进群消息/表格。
    try:
        if row.kind == "recall":
            for r in (host._recall_history.get(row.group) or []):
                if int(r.get("id") or -1) == row.id:
                    item["content"] = str(r.get("content") or "")
                    break
        elif row.kind == "verdict":
            for r in host.mod.list_verdicts(group=row.group, limit=100000):
                if int(r.get("id") or -1) == row.id:
                    item["content"] = str(r.get("content") or "")
                    break
    except Exception as e:
        host.logger.warning(f"读取记录原文失败: {e}")
    return json_response({"item": item})
