"""踢人指令：/群管 踢人 [拉黑] @成员... [理由] / /群管 踢人 记录 [N]"""

from __future__ import annotations

import time

from . import record as kick_record

MAX_BATCH = 20


def _mentions(event) -> list[dict]:
    """从原始消息里取被 @ 的成员（排除机器人自己）。"""
    raw = getattr(event.message_obj, "raw_message", None)
    mentions = getattr(raw, "mentions", None)
    if mentions is None and isinstance(raw, dict):
        mentions = raw.get("mentions")
    out, seen = [], set()
    for u in mentions or []:
        if isinstance(u, dict):
            oid = str(u.get("member_openid") or u.get("id") or "")
            name = u.get("member_nickname") or u.get("username") or ""
            is_bot = bool(u.get("bot")) or bool(u.get("is_you"))
        else:
            oid = str(getattr(u, "member_openid", "") or getattr(u, "id", "") or "")
            name = getattr(u, "member_nickname", "") or getattr(u, "username", "") or ""
            is_bot = bool(getattr(u, "bot", False)) or bool(getattr(u, "is_you", False))
        if not oid or is_bot or oid in seen:
            continue
        seen.add(oid)
        out.append({"openid": oid, "name": str(name).strip() or oid[-6:]})
    return out


def _fmt_line(r: dict) -> str:
    ts = time.strftime("%m-%d %H:%M", time.localtime(float(r.get("ts") or 0)))
    targets = "、".join(t.get("name") or t.get("openid", "")[-6:] for t in r.get("targets") or [])
    state = "成功" if r.get("ok") else f"失败({r.get('err_code') or '-'})"
    extra = ""
    reason = f"，理由：{r['reason']}" if r.get("reason") else ""
    return f"#{r.get('id')} {ts} {targets}｜{state}{extra}{reason}"


async def cmd_kick(host, event, parts: list[str] | None = None) -> None:
    parts = list(parts or [])
    args = parts[1:]  # 去掉 "/踢人"
    group = event.get_group_id()

    if args and args[0] in ("记录", "历史", "history"):
        limit = 10
        if len(args) > 1 and str(args[1]).isdigit():
            limit = int(args[1])
        rows = kick_record.recent(host, group, limit)
        if not rows:
            await host._reply(event, host._render("msg_kick_history_empty"))
            return
        await host._reply(event, host._render("msg_kick_history_title", count=len(rows),
                                                   lines="\n".join(_fmt_line(r) for r in rows)))
        return

    targets = _mentions(event)
    if not targets:
        await host._reply(event, host._render("msg_kick_usage"))
        return
    if len(targets) > MAX_BATCH:
        await host._reply(event, host._render("msg_kick_too_many", max=MAX_BATCH))
        return

    reason = " ".join(args).strip()

    operator = event.get_sender_id() or ""
    operator_name = event.get_sender_name() or operator
    try:
        api = host._get_api(event.get_platform_id())
        resp = await api.batch_remove_members(group, [t["openid"] for t in targets])
        ok = str((resp or {}).get("remove_members_result") or "") == "success"
        rec = kick_record.add(host, {
            "group": group, "operator": operator, "operator_name": operator_name,
            "targets": targets, "reason": reason,
            "ok": ok, "result": resp if isinstance(resp, dict) else {},
        })
        names = "、".join(t["name"] for t in targets)
        ctx = host._ctx(event.get_platform_id(), group, operator)
        host.logger.info(f"{ctx}[踢人] 成功 目标={names} 人数={len(targets)} 记录#{rec['id']} 理由={reason or '-'}")
        await host._reply(event, host._render("msg_kick_success", names=names, id=rec["id"]))
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        kick_record.add(host, {
            "group": group, "operator": operator, "operator_name": operator_name,
            "targets": targets, "reason": reason,
            "ok": False, "err_code": code, "err_msg": str(e)[:200],
        })
        ctx = host._ctx(event.get_platform_id(), group, operator)
        host.logger.warning(f"{ctx}[踢人] 失败 code={code} 目标={len(targets)}人 记录已留档 原因={str(e)[:120]}")
        perm_key = {
            "40103012": "msg_kick_perm_owner",
            "40103011": "msg_kick_perm_bot",
            "11253": "msg_kick_no_permission",
        }.get(str(code))
        if perm_key:
            await host._reply(event, host._render(perm_key))
        else:
            await host._reply(event, host._render("msg_kick_fail", error=str(e)[:120]))
