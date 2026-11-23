"""`/群管 记录`：插件处理记录查询。"""
from __future__ import annotations

import time

from .query import get as get_record, iter_rows, query as query_records


_TYPE_ALIASES = {
    "判定": "verdict", "审核": "verdict", "verdict": "verdict",
    "禁言": "mute", "mute": "mute",
    "撤回": "recall", "recall": "recall",
    "踢人": "kick", "kick": "kick",
}
_TIME_ALIASES = {"今天": 1, "today": 1, "7天": 7, "7d": 7, "七天": 7}
_COVERAGE = "仅记录本插件执行的操作；其他管理员的撤回/禁言不在此列。"


def _esc(text, limit: int = 18) -> str:
    s = str(text or "").replace("|", "｜").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _when(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "-"


def _target(row) -> str:
    return row.target_name or (row.target_id[-6:] if row.target_id else "-")


def _action(row) -> str:
    action = {"mute": "禁言", "recall": "撤回", "kick": "踢人"}.get(row.action, "判定")
    return f"{action} · {row.result}"


def _table(rows) -> str:
    head = "| 记录 | 时间 | 目标 | 处理 |\n| --- | --- | --- | --- |"
    body = "\n".join(
        f"| `{r.display_id}` | {_when(r.ts)} | {_esc(_target(r))} | {_esc(_action(r), 22)} |"
        for r in rows
    )
    return f"### 插件处理记录\n\n{head}\n{body}\n\n> {_COVERAGE}"


def _detail(row, is_admin: bool) -> str:
    lines = [
        f"### 记录 `{row.display_id}`",
        f"- 时间：{_when(row.ts)}",
        f"- 目标：{_target(row)}",
        f"- 处理：{_action(row)}",
        f"- 结果：{row.result}",
    ]
    if row.reason:
        lines.append(f"- 理由：{row.reason}")
    if row.detail.get("category_public"):
        lines.append(f"- 类别：{row.detail['category_public']}")
    if row.detail.get("rule_name"):
        lines.append(f"- 依据：{row.detail['rule_name']}")
    if row.detail.get("error_code"):
        lines.append(f"- 错误码：{row.detail['error_code']}")
    if is_admin and row.actor_name:
        lines.append(f"- 操作人：{row.actor_name}")
    if row.legacy:
        lines.append("- 备注：历史记录缺少目标 ID，仅管理员可按群查看")
    lines.append(f"\n> {_COVERAGE}")
    return "\n".join(lines)


async def _send_markdown(host, event, text: str):
    """记录表必须按 Markdown 发；失败退回纯文本，不吞消息。"""
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Plain

    try:
        chain = MessageChain([Plain(text)])
        chain.use_markdown(True)
        await event.send(chain)
    except Exception as e:
        host.logger.warning(f"记录 Markdown 发送失败，降级纯文本: {e}")
        await host._reply(event, text)


def _parse(args):
    """返回 dict：mine/kind/detail/summary/target_limit/since。"""
    mine = bool(args and args[0].lower() in ("我的", "自己", "mine", "me"))
    if mine:
        args = args[1:]
    detail = None
    summary = False
    kind = None
    limit = 10
    days = 0
    if args and args[0].lower() in ("详情", "detail"):
        detail = args[1] if len(args) > 1 else ""
        return {"mine": mine, "detail": detail, "summary": False, "kind": None, "limit": 0, "days": 0}
    if args and args[0].lower() in ("汇总", "summary"):
        summary = True
        args = args[1:]
    if args and args[0] in _TYPE_ALIASES:
        kind = _TYPE_ALIASES[args[0]]
        args = args[1:]
    for a in args:
        low = a.lower()
        if low in _TIME_ALIASES:
            days = _TIME_ALIASES[low]
        elif a.isdigit():
            limit = max(1, min(50, int(a)))
    return {"mine": mine, "detail": detail, "summary": summary, "kind": kind, "limit": limit, "days": days}


async def cmd_records(host, event, parts):
    args = list(parts[1:]) if parts and str(parts[0]).startswith("/") else list(parts)
    opt = _parse(args)
    group = str(event.get_group_id() or "")
    sender = str(event.get_sender_id() or "")
    is_admin = bool(host._is_admin(event))

    if opt["detail"] is not None:
        row = get_record(host, str(opt["detail"]), group=group)
        if not row:
            await host._reply(event, "没有找到这条记录。")
            return
        if not is_admin and sender not in row.target_ids:
            await host._reply(event, "你只能查看自己的记录。")
            return
        await _send_markdown(host, event, _detail(row, is_admin))
        return

    if opt["summary"]:
        if not is_admin:
            await host._reply(event, "汇总仅管理员可用。")
            return
        since = time.time() - opt["days"] * 86400 if opt["days"] else 0.0
        rows = [
            r for r in iter_rows(host, group=group)
            if (not opt["kind"] or r.kind == opt["kind"]) and (not since or r.ts >= since)
        ]
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.kind] = counts.get(r.kind, 0) + 1
        names = {"verdict": "判定", "mute": "禁言", "recall": "撤回", "kick": "踢人"}
        lines = [f"### 近 {opt['days'] or '全部'} 天插件处理记录"]
        lines += [f"- {names.get(k, k)}：{v} 条" for k, v in counts.items()] or ["- 暂无记录"]
        lines.append(f"\n> {_COVERAGE}")
        await _send_markdown(host, event, "\n".join(lines))
        return

    if not is_admin and not opt["mine"]:
        await host._reply(event, "普通成员请用：/群管 记录 我的 [类型] [N]")
        return

    target_id = sender if opt["mine"] else ""
    if is_admin and not opt["mine"]:
        mentioned = host._mentioned_openids(event)
        if mentioned:
            target_id = mentioned[0]
    since = time.time() - opt["days"] * 86400 if opt["days"] else 0.0
    kinds = [opt["kind"]] if opt["kind"] else None
    rows = query_records(
        host, group=group, kinds=kinds, target_id=target_id, since=since, limit=opt["limit"]
    )
    if not rows:
        await host._reply(event, f"没有找到记录。\n\n{_COVERAGE}")
        return
    await _send_markdown(host, event, _table(rows))
