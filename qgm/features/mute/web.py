"""禁言/撤回历史 Web API。"""

from __future__ import annotations

from astrbot.api.web import json_response, request

from ...kernel.timefmt import fmt_ts
try:
    from ....qq_api import QQApiError
except ImportError:
    from qq_api import QQApiError  # type: ignore


async def web_mutes(host):
        """GET /mutes?group_openid=xxx 当前禁言状态（实时查 QQ）。

        不传 group_openid（或传 all）＝把每个已知群都查一遍并带群名汇总；传具体群
        就只看那个群。多群 / 多机器人时前端靠 group_display / bot 字段区分。
        """
        sel, err = host._pick_groups(request.query.get("group_openid", type=str) or "")
        if err:
            return json_response(err)
        members: list[dict] = []
        errors: list[dict] = []
        group_rules: list[dict] = []
        for e in sel:
            g, pid = e["group_openid"], e["platform_id"]
            label = e.get("display") or g[:12]
            try:
                data = await host._get_api(pid).get_mute_state(g)
            except QQApiError as ex:
                errors.append({"group_openid": g, "group_display": label, "error": str(ex)})
                continue
            rule = data.get("global_rule") or {}
            summary = host._mute_rule_summary(rule)
            if summary["mode"] not in ("", "none"):
                group_rules.append({"group_openid": g, "group_display": label, **summary})
            for m in data.get("members") or []:
                row = dict(m)
                row["group_openid"] = g
                row["group_display"] = label
                row["bot"] = e.get("bot") or ""
                members.append(row)
        single = len(sel) == 1
        return json_response({
            "scope": "group" if single else "all",
            "group_openid": sel[0]["group_openid"] if single else "",
            "platform_id": sel[0]["platform_id"] if single else "",
            "group_display": (sel[0].get("display") or "") if single else "",
            "global_rule": group_rules[0] if (single and group_rules) else None,
            "groups": host._group_entries(),
            "selected": [e["group_openid"] for e in sel],
            "members": members,
            "group_rules": group_rules,
            "errors": errors,
        })

async def web_mute_history(host):
        """GET /history?group_openid=xxx&limit=50 查询禁言历史（倒序，最新在前）。"""
        host._sweep_expired_mutes()  # 顺手把到期的 active 更新成 expired
        limit = request.query.get("limit", 50, type=int)
        limit = max(1, min(limit, 200))
        g = request.query.get("group_openid", type=str) or ""
        records: list[dict] = []
        groups = [g] if g else list(host._mute_history.keys())
        for gg in groups:
            records.extend(host._mute_history.get(gg, []))
        records.sort(key=lambda r: r.get("mute_at", 0), reverse=True)
        out = []
        for r in records[:limit]:
            out.append({
                "id": r.get("id"),
                "group_openid": r.get("group_openid"),
                "platform_id": r.get("platform_id"),
                "member_openid": r.get("member_openid"),
                "username": r.get("username", "未知成员"),
                "reason": r.get("reason", ""),
                "duration_text": r.get("duration_text", ""),
                "seconds": r.get("seconds", 0),
                "mute_at": r.get("mute_at", 0),
                "mute_at_text": fmt_ts(r.get("mute_at", 0)),
                "expire_at": r.get("expire_at", 0),
                "expire_at_text": fmt_ts(r.get("expire_at", 0)),
                "operator": r.get("operator", ""),
                "operator_name": r.get("operator_name", ""),
                "status": host._effective_mute_status(r),
                "unmute_at": r.get("unmute_at"),
                "unmute_at_text": fmt_ts(r.get("unmute_at", 0)) if r.get("unmute_at") else "",
            })
        return json_response({
            "total": len(records),
            "returned": len(out),
            "records": out,
        })

async def web_mute_history_clear(host):
        """POST /history/clear body={'group_openid': 'xxx'} 清空历史（不传群则清空全部）。"""
        payload = await request.json(default={})
        g = payload.get("group_openid") or request.query.get("group_openid", type=str) or ""
        n = 0
        if g:
            n = len(host._mute_history.pop(g, []))
        else:
            n = sum(len(v) for v in host._mute_history.values())
            host._mute_history.clear()
        host._save_mute_history()
        host.logger.info(f"WebUI 清空禁言历史: {n} 条")
        return json_response({"cleared": n})

async def web_mute_history_delete(host):
        """POST /history/delete body={'id': 123} 删除单条记录。"""
        payload = await request.json(default={})
        rid = payload.get("id")
        deleted = 0
        for g, lst in host._mute_history.items():
            for i, r in enumerate(lst):
                if r.get("id") == rid:
                    del lst[i]
                    deleted = 1
                    break
            if deleted:
                break
        if deleted:
            host._save_mute_history()
            host.logger.info(f"WebUI 删除禁言历史记录: id={rid}")
        return json_response({"deleted": deleted})

async def web_recalls(host):
        """GET /recalls?group_openid=xxx&limit=50 撤回历史（不传群则全部群）。"""
        limit = request.query.get("limit", 100, type=int)
        limit = max(1, min(limit, 200))
        g = request.query.get("group_openid", type=str) or ""
        records: list[dict] = []
        if g:
            records = list(host._recall_history.get(g, []))
        else:
            for lst in host._recall_history.values():
                records.extend(lst)
        records.sort(key=lambda r: r.get("recalled_at", 0), reverse=True)
        out = []
        for r in records[:limit]:
            out.append({
                "id": r.get("id"),
                "group_openid": r.get("group_openid"),
                "platform_id": r.get("platform_id"),
                "message_id": r.get("message_id"),
                "content": r.get("content", ""),
                "source": r.get("source", ""),
                "recalled_at": r.get("recalled_at", 0),
                "recalled_at_text": fmt_ts(r.get("recalled_at", 0)),
                "operator": r.get("operator", ""),
                "operator_name": r.get("operator_name", ""),
            })
        return json_response({
            "total": len(records),
            "returned": len(out),
            "records": out,
        })

async def web_recalls_clear(host):
        """POST /recalls/clear body={'group_openid': 'xxx'} 清空撤回历史（不传群则清空全部）。"""
        payload = await request.json(default={})
        g = payload.get("group_openid") or request.query.get("group_openid", type=str) or ""
        n = 0
        if g:
            n = len(host._recall_history.pop(g, []))
        else:
            n = sum(len(v) for v in host._recall_history.values())
            host._recall_history.clear()
        host._save_recall_history()
        host.logger.info(f"WebUI 清空撤回历史: {n} 条")
        return json_response({"cleared": n})

async def web_recalls_delete(host):
        """POST /recalls/delete body={'id': 123} 删除单条撤回记录。"""
        payload = await request.json(default={})
        rid = payload.get("id")
        deleted = 0
        for g, lst in host._recall_history.items():
            for i, r in enumerate(lst):
                if r.get("id") == rid:
                    del lst[i]
                    deleted = 1
                    break
            if deleted:
                break
        if deleted:
            host._save_recall_history()
            host.logger.info(f"WebUI 删除撤回历史记录: id={rid}")
        return json_response({"deleted": deleted})
