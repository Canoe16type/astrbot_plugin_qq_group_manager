"""成员入群 / 退群事件处理。

从 main.py 机械搬迁；依赖通过 host 注入，行为与 1.6.0 一致。
"""

from __future__ import annotations

import time

try:
    from .... import button_kit
except ImportError:
    import button_kit  # type: ignore

try:
    from .... import member_registry as member_registry_lib
except ImportError:  # 测试把插件目录直接加入 sys.path 时
    import member_registry as member_registry_lib  # type: ignore


async def on_member_remove(host, data: dict, platform_id: str | None = None) -> None:
        """成员退群/被移出：先查档案，再按配置在群内发言通知。

        Args:
            data: 网关事件体（含 group_openid / member_openid / user_openid，
                以及 member_event.py 补进来的 _event_id）。
            platform_id: 事件来源的 qq_official 实例 ID（member_event.py 注入
                时通过闭包传入），用于多实例下精确路由 API 客户端。
        """
        try:
            g = str(data.get("group_openid") or "")
            member = str(data.get("member_openid") or "")
            if not g or not member:
                host.logger.warning(f"{host._ctx(platform_id or '-', data.get('group_openid'), data.get('member_openid'))}[成员] 退群事件缺少必要字段")
                return
            if not host._group_in_scope(g, platform_id):
                return

            ev_id = host._member_event_key(g, member, data)
            ctx = host._ctx(platform_id or "-", g, member)
            if host._member_event_seen(ev_id):
                host.logger.debug(f"{ctx}[成员] 重复的退群事件，已忽略: {ev_id}")
                return
            ev_ts = float(data.get("timestamp") or time.time())
            if host.members.is_tombstoned(g, member, ev_ts):
                # 档案已被管理员清理，这是重投/补发的旧事件：不再重建，也不再发通知
                host.logger.info(f"{ctx}[成员] 退群事件早于清理记录，跳过重建")
                return

            # 1) 先查档案（此时 status 还是 active，能算在群时长）
            rec = host.members.get(g, member)
            joined_at = float((rec or {}).get("first_seen") or 0)
            now = float(data.get("timestamp") or time.time())
            msgs = int((rec or {}).get("msg_count") or 0)
            username = str((rec or {}).get("nickname") or "").strip()
            name_src = "existing" if username else ""
            user_openid = str(data.get("user_openid") or "")

            # 2a) 跨群认人：事件里的 user_openid 在同一机器人下跨群稳定，
            #     别的群补过名字就直接借过来（source 记为 cross）。
            if not username and user_openid:
                got, from_group = host.members.name_by_user_openid(user_openid, exclude_group=g)
                if got:
                    username = got
                    name_src = "cross"
                    host.logger.debug(
                        f"{host._ctx(platform_id or '-', g, member)}[成员] 借用群 {host._lb(from_group)} 的名字"
                    )

            # 2b) 还没有名字，再试一次接口（白名单机器人可用，普通机器人报 11253）
            if not username and bool(host._read_config_value(
                ("member_event", "member_remove_try_fetch_name"), default=True,
                legacy_keys=("member_remove_try_fetch_name",),
            )):
                fetched = await host._resolve_member_name(g, member)
                if fetched:
                    username = fetched
                    name_src = "api"

            # 3) 落档：有档案标退群，没档案也建一条「只有 openid」的记录便于追溯
            if host._members_on():
                try:
                    if rec is None:
                        rec = host.members.create_left(g, member, ts=now)
                        if user_openid:
                            host.members.bind_user_openid(g, member, user_openid)
                    else:
                        host.members.mark_left(g, member, ts=now)
                        if user_openid:
                            host.members.bind_user_openid(g, member, user_openid)
                except Exception as e:  # noqa: BLE001
                    host.logger.debug(f"{host._ctx(platform_id or '-', g, member)}[成员] 标记退群失败: {e}")
                # 刚补到的名字（跨群借的 / 接口查的）写回本群档案
                if username and name_src in ("cross", "api"):
                    try:
                        cur = host.members.get(g, member) or {}
                        if not str(cur.get("nickname") or "").strip():
                            host.members.set_name(g, member, username, source=name_src)
                    except Exception as e:  # noqa: BLE001
                        host.logger.debug(f"{host._ctx(platform_id or '-', g, member)}[成员] 回写昵称失败: {e}")

            auto_delete = bool(host._read_config_value(
                ("member_event", "auto_delete_on_leave"), default=False,
                legacy_keys=("auto_delete_on_leave",),
            ))
            if auto_delete and host._members_on():
                try:
                    host.members.delete(g, member)          # 写墓碑，防止重投事件重建
                    host.logger.info(
                        f"{ctx}[成员] 退群，档案已自动删除"
                    )
                except Exception as e:  # noqa: BLE001
                    host.logger.warning(f"{host._ctx(platform_id or '-', g, member)}[成员] 自动删除失败: {e}")
            elif host._members_on():
                host.logger.info(
                    f"{ctx}[成员] 已标记「已退」（档案保留；清理用 /群管 成员 清理）"
                )

            if not bool(host._read_config_value(
                ("member_event", "enable_member_remove_notify"),
                default=False,
                legacy_keys=("enable_member_remove_notify",),
            )):
                return

            # 4) 组通知文案
            days = int(max(0, now - joined_at) // 86400) if joined_at else 0
            extra = ""
            if joined_at:
                extra = f"\n在群 {days} 天 · 共发言 {msgs} 条"
            text = host._render(
                "msg_member_remove_notify",
                username=username or "某位成员",
                short=member_registry_lib.short_id(member),
                openid=member,
                at=member,
                days=days,
                msgs=msgs,
                extra=extra,
                group=(host.mod.get_group_meta(g).get("name") or g),
            )
            if not str(text or "").strip():
                return

            api = host._api_for_group_or_default(g, fallback_pid=platform_id)
            if api is None:
                host.logger.warning(f"{host._ctx(platform_id or '-', g, member)}[成员] 无可用 API 客户端，无法发送退群通知")
                return

            # 5) 发送。官方「支持 event_id 被动回复」的事件只有
            #    INTERACTION_CREATE / GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE，
            #    GROUP_MEMBER_REMOVE 不在名单里（会返回 40034027），
            #    所以默认直接发主动消息；想试被动回复的用户可开 passive_first，
            #    失败会自动降级，不会把通知丢掉。
            mode = str(host._read_config_value(
                ("member_event", "member_remove_notify_mode"), default="active"
            ) or "active")
            event_id = str(data.get("_event_id") or "")
            sent = False
            if mode == "passive_first" and event_id:
                try:
                    await api.send_group_message(g, text, event_id=event_id)
                    sent = True
                    host.logger.info(f"{ctx}[成员] 退群通知已用 event_id 被动回复")
                except Exception as e:  # noqa: BLE001
                    code = getattr(e, "code", None)
                    host.logger.info(
                        f"{host._ctx(platform_id or '-', g, member)}[成员] 被动回复失败(code={code})，降级为主动消息: {e}"
                    )
            if not sent:
                try:
                    await api.send_group_message(g, text)
                    host._member_perm_blocked.discard(g)
                    host.logger.info(
                        f"{host._ctx(platform_id or '-', g, member)}[成员] 退群，已发送主动消息通知"
                    )
                except Exception as e:  # noqa: BLE001
                    code = getattr(e, "code", None)
                    hint = host._explain_send_error(code)
                    blocked = False
                    try:
                        blocked = g in host._member_perm_blocked      # 先判断"之前是否已经提示过"
                        if int(code) == 40034105:
                            host._member_perm_blocked.add(g)
                    except (TypeError, ValueError):
                        # noqa: silent-cleanup —— 清理/权限尽力而为，失败不影响主流程
                        pass
                    if blocked:
                        # 已经提示过该群没开主动消息权限，后续同类失败不再刷 ERROR
                        host.logger.debug(
                            f"{ctx}[成员] 主动消息仍无权限(code={code})，跳过通知"
                        )
                    else:
                        host.logger.warning(
                            f"{ctx}[成员] 退群通知发送失败(code={code if code is not None else '未知'})："
                            f"{hint}（该群后续同类失败仅记 DEBUG，权限开通后自动恢复）"
                        )
        except Exception as e:  # noqa: BLE001
            host.logger.exception(f"{ctx if 'ctx' in locals() else host._ctx(platform_id or '-', data.get('group_openid') or '')}[成员] 处理退群事件异常: {e}")


async def on_member_add(host, data: dict, platform_id: str | None = None) -> None:
        """成员入群（GROUP_MEMBER_ADD）：建档案 + 发欢迎语。

        事件体只有 timestamp / group_openid / member_openid（官方文档如此，
        没有昵称、没有 user_openid、外层虽带事件 id 但该事件不在被动回复
        白名单里），所以欢迎语只能走主动消息。
        """
        g = str(data.get("group_openid") or "")
        member = str(data.get("member_openid") or "")
        if not g or not member:
            host.logger.warning(f"{host._ctx(platform_id or '-', data.get('group_openid'), data.get('member_openid'))}[成员] 入群事件缺少必要字段")
            return
        if not host._group_in_scope(g, platform_id):
            return
        try:
            host.members.clear_tombstone(g, member)   # 重新入群：允许重建档案
        except Exception:
            # noqa: silent-cleanup —— 降级兜底，不影响主流程
            pass
        ctx = host._ctx(platform_id or "-", g, member)
        ev_id = host._member_event_key(g, member, data)
        if host._member_event_seen(ev_id):
            host.logger.debug(f"{ctx}[成员] 重复的入群事件，已忽略: {ev_id}")
            return

        # 1) 档案：入群瞬间就能建（只有 openid），退群时「在群多久」才有据可查
        if host._members_on():
            try:
                rec = host.members.seen(
                    g, member,
                    ts=data.get("timestamp") or None,
                    count_msg=False,
                    user_openid=str(data.get("user_openid") or ""),
                    group_nickname=str(data.get("member_nickname") or data.get("group_nickname") or ""),
                )
                host.logger.info(
                    f"{host._ctx(platform_id or '-', g, member)}[成员] 入群建档"
                    f"（{'已在档' if rec and rec.get('first_seen') else '新建'}）"
                )
            except Exception as e:  # noqa: BLE001
                host.logger.debug(f"{host._ctx(platform_id or '-', g, member)}[成员] 入群建档失败: {e}")

        # 2) 欢迎语：不受「成员档案」开关影响，单独由 member_event 配置控制
        try:
            await host._welcome_member_on_join(g, member, platform_id)
        except Exception as e:  # noqa: BLE001
            host.logger.exception(f"{host._ctx(platform_id or '-', g, member)}[入群欢迎] 处理异常: {e}")


async def _on_group_join_request(host, data, platform_id=None):
        """GROUP_JOIN_REQUEST 事件入口（由 member_event 注入的 handler 调过来）。"""
        if not isinstance(data, dict):
            return
        group_openid = str(data.get("group_openid") or "")
        member_openid = str(data.get("member_openid") or "")
        if not group_openid or not member_openid:
            host.logger.debug(f"[入群申请] 事件字段不完整，忽略: {list(data)[:6]}")
            return
        if not host._group_in_scope(group_openid, platform_id):
            host.logger.debug(f"[入群申请] 群 {group_openid[-6:]} 不在管理范围，忽略")
            return

        now = time.time()
        host._join_event_at[group_openid] = now
        host._join_event_last = now
        host._join_event_total += 1

        apply_at = str(data.get("apply_at") or "")
        req = {
            "join_request_id": str(data.get("join_request_id") or ""),
            "member_openid": member_openid,
            "username": str(data.get("username") or ""),
            "apply_at": apply_at,
            "apply_source": str(data.get("apply_source") or ""),
            "invited_by": str(data.get("invited_by") or ""),
            "risk_tips": str(data.get("risk_tips") or ""),
            "verify_info": data.get("verify_info") or {},
            "bot": bool(data.get("bot")),
            "_from": "event",
        }
        is_new = host._upsert_pending(group_openid, req)
        if req["username"]:
            try:
                host._nick_cache.setdefault(group_openid, {})[member_openid] = req["username"]
            except Exception:
                # noqa: silent-cleanup —— 降级兜底，不影响主流程
                pass

        # 自动审批策略已经放行：这不是「待审批」，只发一条结果提示（可关）。
        auto = data.get("auto_approved") if isinstance(data, dict) else None
        if isinstance(auto, dict) and auto.get("strategy_id"):
            strategy = str(auto.get("strategy_id"))
            host.logger.info(
                f"[入群申请] 群 {group_openid[-6:]} 成员 {member_openid[-6:]} 已被策略 {strategy} 自动通过"
            )
            # 自动通过 = 不需要人工处理，从待审批缓存里摘掉
            host._pending[group_openid] = [
                x for x in host._pending.get(group_openid, [])
                if host._req_key(x) != host._req_key(req)
            ]
            if bool(host._read_config_value(
                ("approval", "notify_auto_approved"), default=True,
                legacy_keys=("notify_auto_approved",),
            )):
                text = host._render(
                    "msg_join_request_auto_approved",
                    username=req["username"] or member_openid[-6:],
                    strategy=strategy,
                    group_name=group_openid,
                )
                if await host._send_group_proactive(
                    group_openid, text, platform_id or host._known_groups.get(group_openid, "")
                ):
                    host.logger.info(f"[入群申请] 已推送自动通过提示到群 {group_openid[-6:]}")
            return

        host.logger.info(
            f"[入群申请] 群 {group_openid[-6:]} 收到申请"
            f"（{'新申请' if is_new else '刷新已有申请'}）"
            f" member={member_openid[-6:]} source={req['apply_source'] or '?'}"
        )
        if not is_new:
            # 同一申请重复推送（断线重连补事件）：缓存已更新，通知去重交给 _notified。
            pass
        if not host._join_notify_enabled():
            return
        if is_new:
            # 进合并队列，2 秒内的多条申请一起发（见 _flush_join_notify）
            host._join_fresh.setdefault(group_openid, []).append(req)
            host._schedule_join_notify(group_openid, platform_id)
        else:
            # 重复推送（断线补事件）：缓存已刷新，通知窗口内不再重复发，
            # 但如果是「上一批还没发出」的新申请，仍然让它照常合并发送。
            if host._join_fresh.get(group_openid):
                host._schedule_join_notify(group_openid, platform_id)

async def _notify_join_requests(host, group_openid, platform_id, reqs, *, source: str = "轮询"):
        """把一批新申请推送到群里（事件与轮询共用），返回实际推送的条数。

        统一出口的好处：按钮、去重、失败降级只有一份实现——
        之前事件/轮询各写一套的话，按钮很容易只在其中一条路径上生效。
        """
        if not reqs:
            return 0
        api = host._api_for_group_or_default(group_openid, platform_id or "")
        if api is None:
            host.logger.warning(
                f"[入群申请] 群 {group_openid[-6:]} 找不到可用 API 客户端，通知跳过"
            )
            return 0

        now = time.time()
        dedupe_sec = host.JOIN_NOTIFY_DEDUPE_SECONDS
        notified = host._notified.setdefault(group_openid, {})
        fresh = [
            r for r in reqs
            if now - notified.get(host._req_key(r), 0) >= dedupe_sec
        ]
        if not fresh:
            host.logger.debug(f"[入群申请] {source}通知：{len(reqs)} 条申请近期已通知过，跳过")
            return 0

        pending = host._pending.get(group_openid) or []
        lines = []
        for r in fresh:
            try:
                idx = next(
                    i + 1 for i, x in enumerate(pending) if host._req_key(x) == host._req_key(r)
                )
            except StopIteration:
                idx = len(pending)
            lines.append(host._fmt_request_line(idx, r))
        text = host._render("msg_join_request_notify", list="\n".join(lines))

        buttons_on, perm, limit = host._join_button_options()
        keyboard = None
        if buttons_on and host._button_channel_ready():
            keyboard = button_kit.join_request_keyboard(
                fresh, permission=perm, max_requests=limit
            )
            if len(fresh) > limit:
                host.logger.info(
                    f"[入群申请] {len(fresh)} 条新申请，按钮只带前 {limit} 条"
                    f"（行数上限 {button_kit.MAX_ROWS}），其余请用 /群管 审批 处理"
                )
        elif buttons_on:
            host.logger.info("[入群申请] 当前 AstrBot 版本不支持按钮回调，转为纯文本通知")

        ok = await host._send_group_proactive(
            group_openid, text, platform_id or "", keyboard=keyboard
        )
        if not ok:
            host.logger.warning(
                f"[入群申请] 群 {group_openid[-6:]} 推送申请通知失败"
                f"（QQ 主动消息可能受权限/频控限制，可用 /群管 审批 手动查看）"
            )
            return 0
        for r in fresh:
            notified[host._req_key(r)] = now
        host.logger.info(
            f"[入群申请] 已推送 {len(fresh)} 条申请通知到群 {group_openid[-6:]}"
            f"（来源={source}，按钮={button_kit.describe_keyboard(keyboard) if keyboard else '无'}）"
        )
        return len(fresh)

async def _welcome_member_on_join(host, group_openid, member_openid, platform_id):
        """成员真进群时发欢迎语（主动消息，见 _send_group_proactive）。"""
        if not bool(host._read_config_value(
            ("member_event", "enable_member_add_welcome"), default=True,
            legacy_keys=("enable_member_add_welcome",),
        )):
            host.logger.debug("[入群欢迎] 配置已关闭，跳过")
            return

        now = time.time()
        # 先抢占槽位再发：审批通过路径和入群事件路径是两条独立的并发流程，
        # 谁先到谁发，"检查→发送→写记录"的旧写法在两者交错时必然双发。
        if not host._claim_join_welcome(group_openid, member_openid):
            last = float(
                host._join_welcomed.get(f"{group_openid}:{member_openid}") or 0
            )
            host.logger.info(
                f"[入群欢迎] 群 {str(group_openid)[-6:]} 成员 "
                f"{member_registry_lib.short_id(member_openid)} "
                f"{int(now - last)} 秒前刚欢迎过（事件重推或审批已欢迎），跳过"
            )
            return

        # 名字：进群瞬间档案通常只有 openid，能用就用，实在没有给个中性称呼
        username = ""
        if host._members_on():
            try:
                username = str(
                    (host.members.get(group_openid, member_openid) or {}).get("nickname") or ""
                ).strip()
            except Exception:  # noqa: BLE001
                username = ""

        try:
            group_name = host._welcome_group_name(group_openid)
        except Exception:  # noqa: BLE001
            group_name = group_openid

        text = host._render(
            "msg_welcome_after_approve",
            username=username or "新朋友",
            group_name=group_name,
            bot_name=platform_id or "",
            at=member_openid,
        )
        if not str(text or "").strip():
            host.logger.info("[入群欢迎] 欢迎语模板为空，跳过")
            host._release_join_welcome(group_openid, member_openid)
            return

        ok = await host._send_group_proactive(
            group_openid, text, platform_id or "", at_openid=member_openid
        )
        if ok:
            host.logger.info(
                f"[入群欢迎] 群 {str(group_openid)[-6:]} 已向 "
                f"{member_registry_lib.short_id(member_openid)} 发送欢迎语"
            )
        else:
            # 发失败就释放槽位，下次事件重推还能再试一次
            host._release_join_welcome(group_openid, member_openid)
            host.logger.warning(
                f"[入群欢迎] 群 {str(group_openid)[-6:]} 欢迎语发送失败："
                f"主动消息需要该群允许机器人推送（部分群需群主在权限项里开启），"
                f"也可能撞上频控或用户在客户端关掉了「允许主动发送」"
            )
