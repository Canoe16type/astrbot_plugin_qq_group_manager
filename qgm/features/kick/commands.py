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
    extra = "，已拉黑" if r.get("add_to_blacklist") else ""
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
            await host._reply(event, "本群还没有踢人记录。")
            return
        await host._reply(event, "踢人记录（最近 %d 条）：\n%s" % (len(rows), "\n".join(_fmt_line(r) for r in rows)))
        return

    targets = _mentions(event)
    if not targets:
        await host._reply(event, "用法：/群管 踢人 [拉黑] @成员... [理由]\n查看记录：/群管 踢人 记录 [N]")
        return
    if len(targets) > MAX_BATCH:
        await host._reply(event, f"单次最多移除 {MAX_BATCH} 人，请分批操作。")
        return

    add_to_blacklist = "拉黑" in args
    reason = " ".join(a for a in args if a != "拉黑").strip()

    operator = event.get_sender_id() or ""
    operator_name = event.get_sender_name() or operator
    try:
        api = host._get_api(event.get_platform_id())
        resp = await api.batch_remove_members(
            group, [t["openid"] for t in targets], add_to_blacklist=add_to_blacklist)
        ok = str((resp or {}).get("remove_members_result") or "") == "success"
        rec = kick_record.add(host, {
            "group": group, "operator": operator, "operator_name": operator_name,
            "targets": targets, "reason": reason, "add_to_blacklist": add_to_blacklist,
            "ok": ok, "result": resp if isinstance(resp, dict) else {},
        })
        names = "、".join(t["name"] for t in targets)
        await host._reply(event, f"已移除 {names}（记录 #{rec['id']}）" + ("，并加入黑名单。" if add_to_blacklist else "。"))
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        kick_record.add(host, {
            "group": group, "operator": operator, "operator_name": operator_name,
            "targets": targets, "reason": reason, "add_to_blacklist": add_to_blacklist,
            "ok": False, "err_code": code, "err_msg": str(e)[:200],
        })
        if str(code) == "11253":
            await host._reply(event, "踢人接口暂未开通：该能力目前是 QQ 官方内邀白名单（错误码 11253），请联系平台运营申请权限。")
        else:
            await host._reply(event, f"踢人失败：{str(e)[:120]}")
