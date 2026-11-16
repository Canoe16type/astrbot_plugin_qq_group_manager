"""成员昵称解析与名单渲染。"""

from __future__ import annotations

import time
from datetime import datetime

from ...kernel.timefmt import _active_tz

try:
    from .... import member_registry as member_registry_lib
except ImportError:
    import member_registry as member_registry_lib  # type: ignore


async def _resolve_member_name(host, group_openid, member_openid):
        """尽力获取成员昵称：档案 -> 内存缓存 -> 群成员接口。查不到返回空串。"""
        try:
            rec = host.members.get(group_openid, member_openid) or {}
            if str(rec.get("nickname") or "").strip():
                return str(rec["nickname"]).strip()
        except Exception:  # noqa: BLE001
            # noqa: silent-cleanup —— 解析/读取兜底，失败即跳过
            pass
        cached = (host._nick_cache.get(group_openid) or {}).get(member_openid)
        if cached:
            return cached
        api = host._api_for_group_or_default(group_openid)
        if api is not None:
            try:
                info = await api.get_group_member(group_openid, member_openid)
                name = (info or {}).get("nickname") or (info or {}).get("username") or ""
                if name:
                    host._nick_cache.setdefault(group_openid, {})[member_openid] = name
                    # 接口给了名字就顺手回填档案（人工昵称不会被覆盖）
                    if host._members_on():
                        try:
                            rec = host.members.get(group_openid, member_openid)
                            if rec is not None and rec.get("name_source") != "manual":
                                host.members.set_name(group_openid, member_openid, name, source="api")
                        except Exception:  # noqa: BLE001
                            # noqa: silent-cleanup —— 解析/读取兜底，失败即跳过
                            pass
                    return name
            except Exception:
                # noqa: silent-cleanup —— 解析/读取兜底，失败即跳过
                pass
        return ""

def _member_ref(host, group_openid, token):
        """把「序号」或「openid / openid 尾段」解析成完整 openid。"""
        tok = str(token or "").strip()
        if not tok:
            return ""
        if tok.isdigit() and len(tok) <= 4:
            rows, _ = host.members.list_members(group_openid, limit=200)
            idx = int(tok)
            if 1 <= idx <= len(rows):
                return str(rows[idx - 1].get("openid") or "")
            # 超出范围就不要再当序号了，下面继续按 openid / 尾段找
        if host.members.has(group_openid, tok):
            return tok
        rows, _ = host.members.list_members(group_openid, keyword=tok, limit=5)
        if len(rows) == 1:
            return str(rows[0].get("openid") or "")
        return ""

def _render_member_list(host, group_openid, *, status='all', keyword='', page=1, title=''):
        """档案列表文本（每页 15 条）。"""
        per = 15
        rows, total = host.members.list_members(
            group_openid, status=status, keyword=keyword,
            offset=(page - 1) * per, limit=per,
        )
        st = host.members.stats(group_openid)
        head = title or {
            "active": "在群成员",
            "left": "已退群成员",
            "unnamed": "还没名字的成员",
            "named": "已命名成员",
        }.get(status, "全部成员")
        lines = [
            f"📇 {head}（共 {st['total']} 人：在群 {st['active']} / 已退 {st['left']}；"
            f"有名字 {st['named']}，累计发言 {st['messages']} 条）"
        ]
        if not rows:
            lines.append("这一页没有记录。档案是随着群消息自动攒的，机器人没收到过消息就不会有记录。")
            return "\n".join(lines)
        base = (page - 1) * per
        for i, r in enumerate(rows, start=1):
            name = str(r.get("nickname") or "").strip()
            label = name or f"（未命名 {member_registry_lib.short_id(r.get('openid'))}）"
            note = str(r.get("note") or "").strip()
            last = float(r.get("last_seen") or 0)
            when = (
                datetime.fromtimestamp(last, _active_tz()).strftime("%m-%d %H:%M")
                if last else "-"
            )
            flag = " · 已退群" if r.get("status") == "left" else ""
            extra = f" · 备注：{note}" if note else ""
            lines.append(
                f"{base + i}. {label} · 发言 {int(r.get('msg_count') or 0)} · 末次 {when}{flag}{extra}"
            )
        pages = max(1, (total + per - 1) // per)
        if pages > 1:
            lines.append(f"—— 第 {page}/{pages} 页 · 下一页：/群管 成员 列表 {page + 1}")
        lines.append("改名：/群管 成员 改名 <序号> <昵称>")
        return "\n".join(lines)

def _aggregate_member_stats(host, records):
        """按成员聚合禁言记录：次数、最近时间/状态/理由。禁言中优先、次数倒序。"""
        stats: dict[str, dict] = {}
        for r in records:
            oid = r.get("member_openid") or "?"
            st = stats.setdefault(
                oid,
                {"openid": oid, "count": 0, "last": 0, "status": "", "last_reason": ""},
            )
            st["count"] += 1
            ts = r.get("mute_at") or 0
            if ts >= st["last"]:
                st["last"] = ts
                st["status"] = host._effective_mute_status(r)
                st["last_reason"] = r.get("reason") or ""
        out = list(stats.values())
        out.sort(key=lambda s: (s["status"] != "active", -s["count"]))
        return out

async def _try_fetch_group_name(host, group, pid):
        """入群时顺手拉一次群名。失败不抛异常、不重试（面板「拉取群名」可兜底）。"""
        try:
            api = host._get_api(pid)
            data, err = await api.fetch_group_info(group)
        except Exception as e:                                   # noqa: BLE001
            host.logger.debug(f"[入群事件] 拉群名异常: {e}")
            return ""
        if data and data.get("group_name"):
            name = str(data["group_name"])[:60]
            host.mod.set_group_meta(group, name=name, platform_id=pid,
                                    name_state="ok", name_error="")
            return name
        code = getattr(err, "code", None)
        host.mod.set_group_meta(group, platform_id=pid,
                                name_state="denied" if code == 11253 else "error",
                                name_error=str(err or "未知错误")[:200])
        return ""

async def _check_group_presence(host, group, pid=''):
        """用 bot_state 接口确认机器人还在不在这个群。

        Returns:
            (state, detail)，state 取值：

            - ``in``      —— 接口正常返回了机器人在群的信息；
            - ``absent``  —— 接口明确报「查不到这个群／不在群里」（404、
              `11293 鉴权失败，机器人非群成员`，或文案含「非群成员」等）；
            - ``unknown`` —— 没权限（11253，该接口仅白名单机器人可用）、
              网络失败、频控等，**一律不当作离群**，避免误删用户数据。
        """
        group = str(group or "")
        if not group:
            return "unknown", "群 openid 为空，无法查询"
        try:
            api = host._get_api(pid) if pid else host._api_for_group_or_default(group, "")
        except Exception as e:                       # noqa: BLE001
            return "unknown", f"拿不到 API 客户端: {e}"
        if api is None:
            return "unknown", "没有可用的机器人实例"
        try:
            info, err = await api.get_group_bot_state(group)
        except Exception as e:                       # noqa: BLE001
            return "unknown", f"调用 bot_state 异常: {e}"

        if err is None:
            if not info:
                return "in", "接口无错误但返回空对象（视为在群）"
            role = str(info.get("member_role") or "?")
            joined = str(info.get("joined_at") or "?")
            proactive = info.get("allow_proactive_msg")
            return "in", f"在群（role={role}，入群时间={joined}，主动消息={proactive}）"

        code = getattr(err, "code", None)
        status = getattr(err, "status", None)
        text = str(err)
        if code == 11253:
            return (
                "unknown",
                "bot_state 接口无权限（11253：仅白名单机器人可用），无法确认，按事件为准",
            )
        # 11293 = 「鉴权失败，机器人非群成员」：这是**明确离群**，不是无权限。
        # 平台用 400 而非 404 返回它，且文案里没有「不存在」，早期只看关键词会误判成 unknown。
        if code == 11293:
            return "absent", f"机器人非群成员（{text}）"
        lowered = text.lower()
        absent_hint = (
            status == 404
            or any(
                k in text
                for k in (
                    "不在群", "未在群", "非群成员", "不是群成员", "群不存在", "不存在",
                )
            )
            or any(
                k in lowered
                for k in (
                    "not found", "not in group", "not a member",
                    "not a group member", "invalid group",
                )
            )
        )
        if absent_hint:
            return "absent", f"不在群（{text}）"
        return "unknown", f"查询失败，无法确认（{text}）"
