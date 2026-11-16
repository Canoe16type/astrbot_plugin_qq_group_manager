"""成员档案 Web API。"""

from __future__ import annotations

import re

from astrbot.api.web import json_response, request

from ...kernel.timefmt import fmt_ts
try:
    from .... import member_registry as member_registry_lib
except ImportError:
    import member_registry as member_registry_lib  # type: ignore
try:
    from ....qq_api import QQApiError
except ImportError:
    from qq_api import QQApiError  # type: ignore


async def web_members(host):
        """GET /members?group=xxx&status=&keyword=&sort=&page=&size= 成员档案列表。"""
        g = str(request.query.get("group", "") or "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "missing group"})
        status = str(request.query.get("status", "all") or "all")
        keyword = str(request.query.get("keyword", "") or "")
        sort = str(request.query.get("sort", "last_seen") or "last_seen")
        page = max(1, request.query.get("page", 1, type=int))
        size = min(200, max(5, request.query.get("size", 30, type=int)))
        rows, total = host.members.list_members(
            g, status=status, keyword=keyword, sort=sort,
            offset=(page - 1) * size, limit=size,
        )
        items = []
        for r in rows:
            item = dict(r)
            item["short"] = member_registry_lib.short_id(r.get("openid"))
            item["last_seen_text"] = fmt_ts(r.get("last_seen") or 0)
            item["first_seen_text"] = fmt_ts(r.get("first_seen") or 0)
            item["left_at_text"] = fmt_ts(r.get("left_at") or 0) if r.get("left_at") else ""
            items.append(item)
        return json_response({
            "items": items, "total": total, "page": page, "size": size,
            "stats": host.members.stats(g),
        })

async def web_members_stats(host):
        """GET /members/stats?group=xxx 档案统计。"""
        g = str(request.query.get("group", "") or "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "missing group"})
        return json_response(host.members.stats(g))

async def web_members_update(host):
        """POST /members/update body: {group, openid, nickname?, note?}"""
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        openid = str(payload.get("openid") or "")
        if not g or not openid:
            return json_response({"error": "invalid payload"})
        if not host.members.has(g, openid):
            return json_response({"error": "member not found"})
        if "nickname" in payload:
            host.members.set_name(g, openid, str(payload.get("nickname") or ""), source="manual")
        if "note" in payload:
            host.members.set_note(g, openid, str(payload.get("note") or ""))
        rec = host.members.get(g, openid) or {}
        rec = dict(rec)
        rec["short"] = member_registry_lib.short_id(openid)
        return json_response({"ok": True, "item": rec})

async def web_members_delete(host):
        """POST /members/delete body: {group, openid}"""
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        openid = str(payload.get("openid") or "")
        if not g or not openid:
            return json_response({"error": "invalid payload"})
        ok = host.members.delete(g, openid)
        return json_response({"ok": ok, "stats": host.members.stats(g)})

async def web_members_clear_left(host):
        """POST /members/clear_left body: {group}"""
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "invalid payload"})
        n = host.members.delete_left(g)
        return json_response({"removed": n, "stats": host.members.stats(g)})

async def web_members_import(host):
        """POST /members/import body: {group, text}

        每行一条：`openid,昵称,备注`（逗号/制表符/全角逗号都行），
        最少只需 openid。给"从别处导出一批名字"用。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        text = str(payload.get("text") or "")
        if not g or not text.strip():
            return json_response({"error": "invalid payload"})
        rows = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = [p.strip() for p in re.split(r"[,，\t]+", line)]
            oid = parts[0] if parts else ""
            if not oid:
                continue
            row = {"openid": oid}
            if len(parts) > 1 and parts[1]:
                row["nickname"] = parts[1]
            if len(parts) > 2 and parts[2]:
                row["note"] = parts[2]
            rows.append(row)
        added, updated = host.members.upsert_many(g, rows)
        return json_response({
            "added": added, "updated": updated,
            "parsed": len(rows), "stats": host.members.stats(g),
        })

async def web_members_sync(host):
        """POST /members/sync body: {group} 用官方群成员列表接口补昵称。

        该接口目前是内邀白名单能力（11253），失败时如实返回错误码，
        前端按 code 给提示，不做"看起来成功了"的假象。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "invalid payload"})
        api = host._api_for_group_or_default(g)
        if api is None:
            return json_response({"error": "no_api", "message": "没有可用的机器人实例"})
        try:
            members = await api.list_group_members(g)
        except QQApiError as e:
            return json_response({
                "error": "api_error", "code": e.code,
                "message": "官方群成员列表接口是内邀能力（11253），本机器人暂无权限"
                if e.code == 11253 else str(e),
            })
        except Exception as e:  # noqa: BLE001
            return json_response({"error": "api_error", "message": str(e)})
        rows = [{
            "openid": m.get("member_openid") or "",
            "nickname": m.get("username") or "",
            "role": m.get("member_role") or "",
            "user_openid": m.get("union_openid") or m.get("user_openid") or "",
            "joined_at": m.get("joined_at") or "",
        } for m in members if m.get("member_openid")]
        added, updated = host.members.upsert_many(g, rows)
        return json_response({
            "ok": True, "added": added, "updated": updated,
            "count": len(rows), "stats": host.members.stats(g),
        })
