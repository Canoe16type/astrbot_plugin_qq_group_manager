"""禁言类指令实现：禁言 / 解除 / 名单 / 历史。"""

from __future__ import annotations

from typing import Optional

import re
import time

from ...kernel.timefmt import fmt_duration, fmt_rfc3339, parse_duration
from astrbot.api.message_components import Reply

try:
    from ....qq_api import QQApi, QQApiError
except ImportError:
    from qq_api import QQApi, QQApiError  # type: ignore


async def _cmd_mute(host, event, parts):
        api = host._get_api(event.get_platform_id())
        g = event.get_group_id()
        mentioned = host._mentioned_openids(event)
        if not mentioned:
            await host._reply(event, host._render("msg_mute_need_at"))
            return
        # 时长与理由：第一个能解析为时长的参数作为时长，
        # 其余非 @ 参数全部合并为理由（兼容 "10分钟 刷屏" 与 "刷屏 10分钟" 两种顺序）
        seconds: Optional[int] = None
        reason_parts: list[str] = []
        for p in parts[1:]:
            if p.startswith("@"):
                continue
            if seconds is None:
                s = parse_duration(p)
                if s is not None:
                    seconds = s
                    continue
            reason_parts.append(p)
        if seconds is None:
            await host._reply(event, host._render("msg_duration_invalid"))
            return
        if seconds <= 0:
            await host._reply(event, host._render("msg_duration_invalid"))
            return
        # 官方上限 30 天：超出按上限执行，回复里显示的就是真实时长
        if seconds > 30 * 86400:
            host.logger.info(f"禁言时长 {seconds}s 超过官方上限，按 30 天执行")
            seconds = 30 * 86400
        reason = " ".join(reason_parts).strip() or str(
            host._read_config_value(("basic", "default_mute_reason"), default="违反群规", legacy_keys=("default_mute_reason",))
        )
        target = mentioned[0]
        if target == event.get_sender_id():
            await host._reply(event, host._render("msg_mute_self"))
            return
        expire_ts = time.time() + seconds
        expire_at = QQApi.format_rfc3339(expire_ts)
        host.logger.info(
            f"禁言: group={g} member={target} seconds={seconds} "
            f"reason={reason!r} expire={expire_at}"
        )
        try:
            await api.set_mute(g, target, mute_expire_at=expire_at, op="add")
        except QQApiError as e:
            host.logger.error(f"禁言失败: {e}")
            await host._reply(event, host._render("msg_mute_failed", error=str(e)))
            return
        # 记录历史（被禁言者昵称 API 不返回，存 openid；操作者昵称单独存）
        host._add_mute_record(
            group_openid=g,
            platform_id=event.get_platform_id(),
            member_openid=target,
            username="",
            reason=reason,
            seconds=seconds,
            expire_ts=expire_ts,
            operator=event.get_sender_id(),
            operator_name=event.get_sender_name() or "",
        )
        await host._reply(
            event,
            host._render(
                "msg_mute_success",
                username=f"<@{target}>",
                at=target,
                seconds=seconds,
                minutes=seconds // 60,
                duration_text=fmt_duration(seconds),
                reason=reason,
            ),
        )

async def _cmd_unmute(host, event, parts):
        api = host._get_api(event.get_platform_id())
        g = event.get_group_id()
        mentioned = host._mentioned_openids(event)
        if not mentioned:
            await host._reply(event, host._render("msg_mute_need_at"))
            return
        for target in mentioned:
            try:
                await api.set_mute(g, target, op="del")
                host._mark_unmuted(g, target)
                host.logger.info(f"解禁: group={g} member={target}")
                await host._reply(event, host._render("msg_unmute_success", username=f"<@{target}>", at=target))
            except QQApiError as e:
                host.logger.error(f"解禁失败: {e}")
                await host._reply(event, host._render("msg_mute_failed", error=str(e)))

async def _cmd_list_mutes(host, event, parts):
        api = host._get_api(event.get_platform_id())
        g = event.get_group_id()
        try:
            data = await api.get_mute_state(g)
        except QQApiError as e:
            host.logger.error(f"查询禁言失败: {e}")
            await host._reply(event, host._render("msg_api_error", error=str(e)))
            return
        members = data.get("members") or []
        if not members:
            await host._reply(event, host._render("msg_mute_list_empty"))
            return
        history = list(host._mute_history.get(g, []))
        lines = []
        for m in members:
            oid = m.get("member_openid", "?")
            # QQ API 不返回成员昵称（username 字段基本为空），统一用短标识；
            # 匹配/判断始终用完整 openid，短标识仅用于人眼区分。
            name = (m.get("username") or "").strip() or f"{oid[:8]}"
            expire = fmt_rfc3339(m.get("mute_expire_at", ""))
            reason = host._last_reason(g, oid)
            cnt = sum(1 for r in history if r.get("member_openid") == oid)
            line = f"· {name} 到期 {expire}"
            if reason:
                line += f"（理由：{reason}）"
            if cnt:
                line += f" 历史{cnt}次"
            lines.append(line)
        await host._reply(event, host._render("msg_mute_list", list="\n".join(lines)))

async def _cmd_list_mute_history(host, event, parts):
        """/禁言历史 [条数|汇总] [@用户|openid前缀]：本群流水（默认10条）/ 按人汇总 / 单用户记录。

        单用户查询示例：
          /禁言历史 @某人        -> 该用户最近10条
          /禁言历史 @某人 5      -> 该用户最近5条
          /禁言历史 @某人 汇总   -> 该用户禁言汇总
          /禁言历史 4F697792...  -> 直接输 openid（或前缀）查询，方便查已不在群的用户
        """
        g = event.get_group_id()
        limit = 10
        mode = "group"
        target: Optional[str] = None
        mentioned = host._mentioned_openids(event)
        if mentioned:
            target = mentioned[0]
        for p in parts[1:]:
            if p.isdigit():
                limit = max(1, min(int(p), 50))
            elif p in ("汇总", "summary", "统计"):
                mode = "summary"
            elif target is None and len(p) >= 8 and re.fullmatch(r"[0-9A-Za-z_-]+", p):
                # 未 @ 时支持直接输入 openid 或前缀（条数参数已在上面被 isdigit 捕获）
                target = p
        records = list(host._mute_history.get(g, []))
        if not records:
            await host._reply(event, host._render("msg_mute_history_empty"))
            return

        if target:
            matched = [r for r in records if host._match_openid(r.get("member_openid", ""), target)]
            if not matched:
                await host._reply(
                    event,
                    host._render("msg_mute_history_user_empty", openid=target),
                )
                return
            if mode == "summary":
                await host._reply(
                    event,
                    host._render(
                        "msg_mute_history_user_summary",
                        openid=target,
                        count=len(matched),
                        list=host._render_mute_summary(matched),
                    ),
                )
                return
            matched = matched[-limit:][::-1]  # 最新在前
            blocks = [host._fmt_history_line(r, full=True) for r in matched]
            await host._reply(
                event,
                host._render(
                    "msg_mute_history_user",
                    openid=target,
                    count=len(matched),
                    list="\n\n".join(blocks),
                ),
            )
            return

        if mode == "summary":
            await host._reply(
                event,
                host._render(
                    "msg_mute_history_summary",
                    count=len(records),
                    list=host._render_mute_summary(records),
                ),
            )
            return
        records = records[-limit:][::-1]  # 最新在前
        blocks = [host._fmt_history_line(r) for r in records]
        await host._reply(
            event,
            host._render("msg_mute_history_list", count=len(records), list="\n\n".join(blocks)),
        )


async def _cmd_recall(host, event, parts):
        """撤回消息。

        用法:
        - 引用（回复）目标消息后发送 /撤回
        - /撤回 <message_id>

        官方限制: 发送超过 2 分钟的消息不可撤回；机器人是群管理员时可撤回
        普通群成员的消息（消息 ID 从群消息事件 d.id 获取），否则只能撤回
        自己发送的消息。
        """
        # 功能开关：配置里关闭后指令直接提示（不误报为无权限）
        if not bool(host._read_config_value(
                ("recall", "enable_recall"), default=True,
                legacy_keys=("enable_recall",))):
            await host._reply(event, host._render("msg_recall_disabled"))
            return

        api = host._get_api(event.get_platform_id())
        g = event.get_group_id()
        if not g:
            await host._reply(event, host._render("msg_recall_group_only"))
            return

        message_id = ""
        source = "quote"
        quoted_content = ""
        # 方式一：/撤回 <message_id>
        # 兼容 ROBOT1.0_xxx / REFIDX_xxx== 等官方消息 ID 格式
        if len(parts) > 1 and re.fullmatch(r"[0-9A-Za-z_.\-=]{16,}", parts[1]):
            message_id = parts[1]
            source = "message_id"
        # 方式二：引用（回复）目标消息
        else:
            try:
    
                reply = next(
                    (c for c in event.message_obj.message if isinstance(c, Reply)),
                    None,
                )
                if reply and getattr(reply, "id", None):
                    message_id = str(reply.id)
                    # 引用组件里带被引用消息的原文（官方接口不提供查消息内容，
                    # 引用撤回是唯一能拿到原文的途径），用于历史备份查验。
                    quoted_content = host._clean_quote_summary(getattr(reply, "message_str", "") or "")
                    if not quoted_content:
                        chain = getattr(reply, "chain", None)
                        if chain:
                            quoted_content = "".join(
                                str(getattr(c, "text", "") or "")
                                for c in chain
                            )
                # 无论 Reply 组件是否带原文/ID，都从原始消息里再挖一次：
                # botpy 适配器可能拿不到引用 ID（QQ 官方引用消息的被引用
                # ID 在 msg_elements[0].msg_idx，见 _patch_botpy_quote_fields），
                # 且原文用于下面的缓存匹配（真实消息 ID 只能靠它找回）。
                raw = getattr(event.message_obj, "raw_message", None)
                if raw is not None:
                    mid, mcontent = host._extract_quote_from_raw(raw)
                    if not message_id and mid:
                        message_id = str(mid)
                    if not quoted_content and mcontent:
                        quoted_content = str(mcontent)

                # 关键：QQ 官方引用消息事件拿不到被引用消息的 d.id（只有
                # REFIDX 引用索引，撤回 API 不认），所以优先用原文在消息缓存
                # 里匹配真实消息 ID（机器人能收到群内全量消息时必然命中）。
                if quoted_content:
                    cache_id = host._match_recent_msg_id(g, quoted_content)
                    # 仅当现有 ID 是引用索引（REFIDX/TMP，撤回 API 不接受）
                    # 或为空时，才用缓存中的真实消息 ID 覆盖。
                    if cache_id and (
                        not message_id
                        or message_id.startswith("REFIDX")
                        or message_id.startswith("TMP_")
                    ):
                        message_id = cache_id
                        source = "quote_cache"
                        host.logger.debug(
                            f"引用撤回命中消息缓存: text={quoted_content!r} id={cache_id}"
                        )
                    elif message_id and not cache_id:
                        host.logger.debug(
                            f"引用撤回未命中缓存，将尝试事件内 ID: {message_id} "
                            f"(REFIDX 格式可能不被撤回 API 接受)"
                        )
            except Exception as e:
                host.logger.debug(f"提取引用消息 id 失败: {e}")

        if not message_id:
            await host._reply(event, host._render("msg_recall_need_quote"))
            return

        try:
            await api.recall_group_message(g, message_id)
            host.logger.info(f"撤回消息成功: group={g} message_id={message_id}")
            # 永久保存原始消息，便于备份查验
            rec = host._add_recall_record(
                group_openid=g,
                platform_id=event.get_platform_id(),
                message_id=message_id,
                content=quoted_content,
                source=source,
                operator=event.get_sender_id(),
                operator_name=event.get_sender_name() or "",
            )
            host.logger.info(
                f"已记录撤回历史: id={rec['id']} source={source} "
                f"content={'有原文' if quoted_content else '无原文(仅消息ID)'}"
            )
            await host._reply(event, host._render("msg_recall_ok"))
        except QQApiError as e:
            host.logger.warning(
                f"撤回消息失败: group={g} message_id={message_id} err={e}"
            )
            if e.code == 40064004:
                await host._reply(event, host._render("msg_recall_timeout"))
            elif e.code == 40062003:
                await host._reply(event, host._render("msg_recall_no_perm"))
            elif e.code == 40061001:
                await host._reply(event, host._render("msg_recall_invalid_id"))
            else:
                await host._reply(event, host._render("msg_recall_failed", error=str(e)))
