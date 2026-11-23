"""统一记录 Web API。"""
from __future__ import annotations

import time

from astrbot.api.web import json_response, request

from ..kick import record as kick_record
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
    try:
        days = int(request.query.get("days", 0) or 0)
    except (TypeError, ValueError):
        days = 0
    try:
        limit = int(request.query.get("limit", 20) or 20)
    except (TypeError, ValueError):
        limit = 20
    days = max(0, min(365, days))
    limit = max(1, min(50, limit))
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


async def web_records_clear(host):
    """POST /records/clear body={'group': '', 'kind': ''}；group/kind 为空表示全部。"""
    payload = await request.json(default={})
    group = str(payload.get("group") or "").strip()
    kind = str(payload.get("kind") or "").strip()
    removed = {}
    if kind in ("", "verdict"):
        removed["verdict"] = host.mod.clear_verdicts(group)
    if kind in ("", "mute"):
        n = 0
        if group:
            n = len(host._mute_history.pop(group, []))
        else:
            n = sum(len(v) for v in host._mute_history.values())
            host._mute_history.clear()
        host._save_mute_history()
        removed["mute"] = n
    if kind in ("", "recall"):
        n = 0
        if group:
            n = len(host._recall_history.pop(group, []))
        else:
            n = sum(len(v) for v in host._recall_history.values())
            host._recall_history.clear()
        host._save_recall_history()
        removed["recall"] = n
    if kind in ("", "kick"):
        removed["kick"] = kick_record.clear(host, group)
    total = sum(removed.values())
    host.logger.info(f"WebUI 清空插件记录：{total} 条（{group or '全部群'} / {kind or '全部类型'}）")
    return json_response({"ok": True, "cleared": total, "detail": removed,
                          "group": group, "kind": kind})


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
