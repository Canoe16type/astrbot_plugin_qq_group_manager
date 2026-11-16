"""成员入群 / 退群事件处理。

从 main.py 机械搬迁；依赖通过 host 注入，行为与 1.6.0 一致。
"""

from __future__ import annotations

import time

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
