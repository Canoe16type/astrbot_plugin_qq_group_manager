"""入群审批 Web API。"""

from __future__ import annotations

from datetime import datetime, timezone

from astrbot.api.web import json_response, request

from ...kernel.timefmt import fmt_rfc3339
try:
    from ....qq_api import QQApiError
except ImportError:
    from qq_api import QQApiError  # type: ignore


async def web_join_approval_list(host):
        """GET 策略列表。策略按机器人维度，platform_id 可选。"""
        try:
            pid, perr = host._pick_bot_pid(request.query.get("platform_id", type=str) or "")
            if perr:
                return json_response({"error": perr})
            # 昵称是懒加载的，进审批页时顺手补一次，下拉里就能显示机器人名称而不是 AppID
            if not (host.mod.get_bot_meta(pid) or {}).get("name"):
                await host._refresh_bot_name(pid)
            api = host._get_api(pid)
            data = await api.list_join_approval_strategies(
                cursor=request.query.get("cursor", type=str) or "",
                limit=request.query.get("limit", type=int) or 20,
            )
        except QQApiError as e:
            return json_response({"error": str(e)})
        rows = []
        for s in data.get("strategies") or []:
            gids = [str(x) for x in (s.get("group_ids") or [])]
            gopens = [str(x) for x in (s.get("group_openids") or [])]
            expire_at = s.get("expire_at") or ""
            left_days = None
            try:
                if expire_at:
                    left_days = (datetime.fromisoformat(expire_at.replace("Z", "+00:00"))
                                 - datetime.now(timezone.utc)).days
            except Exception:
                left_days = None
            rows.append({
                "strategy_id": s.get("strategy_id") or "",
                "group_ids": gids,
                "group_openids": gopens,
                "group_mode": "id" if gids else "openid",
                "group_count": len(gids) + len(gopens),
                "whitelist_user_count": s.get("whitelist_user_count") or 0,
                "is_enable": (s.get("is_enable") or "").lower() == "on",
                "expire_at": fmt_rfc3339(expire_at),
                "expire_in_days": left_days,
                "created_at": fmt_rfc3339(s.get("created_at") or ""),
                "updated_at": fmt_rfc3339(s.get("updated_at") or ""),
                "remark": s.get("remark") or "",
            })
        return json_response({
            "platform_id": pid,
            "platforms": host._platform_list(),
            "strategies": rows,
            "next_cursor": data.get("next_cursor") or "",
        })

async def web_join_approval_save(host):
        """POST 创建或修改策略。带 strategy_id＝修改，不带＝创建。"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        raw_groups = str(p.get("groups") or "")
        groups = host._parse_group_input(raw_groups)
        mode = str(p.get("group_mode") or "id")
        is_enable = "on" if p.get("is_enable", True) else "off"
        expire_at = str(p.get("expire_at") or "").strip()
        remark = str(p.get("remark") or "").strip()
        try:
            api = host._approval_api(str(p.get("platform_id") or ""))
            if sid:
                kw: dict = {"is_enable": is_enable}
                if expire_at:
                    kw["expire_at"] = expire_at
                kw["remark"] = remark
                r = await api.update_join_approval_strategy(sid, **kw)
                action = "已更新策略"
                op = str(p.get("group_op") or "").strip()
                if op in ("add", "del") and groups:
                    if mode == "id":
                        try:
                            act: dict = {"op": op, "group_ids": [int(x) for x in groups]}
                        except ValueError:
                            return json_response({"error": "群号必须是纯数字"})
                    else:
                        act = {"op": op, "group_openids": groups}
                    r = await api.update_join_approval_strategy(sid, group_action=act)
                    action = f"已更新策略，并{'新增' if op == 'add' else '移除'} {len(groups)} 个群"
            else:
                if not groups:
                    return json_response({"error": "至少要指定一个群"})
                if mode == "id":
                    try:
                        gids = [int(x) for x in groups]
                    except ValueError:
                        return json_response({"error": "群号必须是纯数字"})
                    r = await api.create_join_approval_strategy(
                        group_ids=gids, is_enable=is_enable,
                        expire_at=expire_at or None, remark=remark or None)
                else:
                    r = await api.create_join_approval_strategy(
                        group_openids=groups, is_enable=is_enable,
                        expire_at=expire_at or None, remark=remark or None)
                action = "已创建策略"
        except QQApiError as e:
            return json_response({"error": str(e)})
        host.logger.info(f"[入群审批] {action} {r.get('strategy_id') or sid}（{len(groups)} 群）")
        return json_response({"ok": True, "result": r, "message": action})

async def web_join_approval_toggle(host):
        """POST 启停策略。"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        if not sid:
            return json_response({"error": "缺少 strategy_id"})
        on = bool(p.get("enabled"))
        try:
            api = host._approval_api(str(p.get("platform_id") or ""))
            r = await api.update_join_approval_strategy(sid, is_enable="on" if on else "off")
        except QQApiError as e:
            return json_response({"error": str(e)})
        host.logger.info(f"[入群审批] 策略 {sid} -> {'启用' if on else '停用'}")
        return json_response({"ok": True, "result": r})

async def web_join_approval_delete(host):
        """POST 删除策略。"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        if not sid:
            return json_response({"error": "缺少 strategy_id"})
        try:
            api = host._approval_api(str(p.get("platform_id") or ""))
            await api.delete_join_approval_strategy(sid)
        except QQApiError as e:
            return json_response({"error": str(e)})
        host.logger.info(f"[入群审批] 已删除策略 {sid}")
        return json_response({"ok": True})

async def web_join_approval_execute(host):
        """POST 立即全量扫描（异步，官方称约 10 分钟）。"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        if not sid:
            return json_response({"error": "缺少 strategy_id"})
        try:
            api = host._approval_api(str(p.get("platform_id") or ""))
            await api.execute_join_approval_strategy(sid)
        except QQApiError as e:
            return json_response({"error": str(e)})
        host.logger.info(f"[入群审批] 已触发全量扫描 {sid}")
        return json_response({"ok": True, "message": "已提交，官方异步执行，约 10 分钟完成"})

async def web_join_approval_whitelist(host):
        """POST 增删白名单号码。body: strategy_id / op(add|del) / users(文本)"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        op = str(p.get("op") or "add")
        users = host._parse_group_input(str(p.get("users") or ""))
        if not sid:
            return json_response({"error": "缺少 strategy_id"})
        if not users:
            return json_response({"error": "白名单号码不能为空"})
        bad = [u for u in users if not u.isdigit()]
        if bad:
            return json_response({"error": f"以下不是纯数字 QQ 号：{', '.join(bad[:5])}"})
        try:
            api = host._approval_api(str(p.get("platform_id") or ""))
            r = await api.update_join_approval_whitelist(sid, op, users)
        except QQApiError as e:
            return json_response({"error": str(e)})
        host.logger.info(f"[入群审批] 白名单 {op} {len(users)} 个 -> {sid}")
        return json_response({"ok": True, "result": r,
                              "message": f"已{'添加' if op == 'add' else '移除'} {len(users)} 个号码"})
