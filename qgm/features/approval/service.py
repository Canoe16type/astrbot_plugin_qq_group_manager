"""审批：决策、请求列表、通知合并、入群欢迎。"""

from __future__ import annotations

import asyncio
import time

from ...kernel.timefmt import fmt_rfc3339

try:
    from .... import button_kit, member_registry as member_registry_lib
except ImportError:
    import button_kit  # type: ignore
    import member_registry as member_registry_lib  # type: ignore
try:
    from ....qq_api import QQApiError
except ImportError:
    from qq_api import QQApiError  # type: ignore


async def _apply_join_decision(host, event, group_openid, platform_id, req, op, *, reason=None, via='指令'):
        """执行一次入群审批，并同步本地缓存（指令与按钮**共用这一份实现**）。

        在 1.4.0 之前，审批逻辑只存在于 ``_cmd_review`` 里；消息按钮上线后如果
        再复制一份，两边很容易漂移（比如一边删了缓存、另一边没删，导致申请
        反复出现在列表里）。所以这里下沉成唯一出口。

        Args:
            event: 来源事件，可为 None（按钮路径也会传，仅用于日志/展示）。
            req: 申请条目（至少含 ``member_openid`` / ``join_request_id``）。
            op: ``approve`` / ``decline``。
            via: 日志里的来源标记（指令 / 按钮）。

        Returns:
            ``(是否成功, 错误描述)``。
        """
        member_openid = str(req.get("member_openid") or "")
        join_request_id = str(req.get("join_request_id") or "")
        api = host._get_api(platform_id)
        try:
            await api.approve_join_request(
                group_openid, member_openid, join_request_id,
                op=op, reject_reason=reason,
            )
        except QQApiError as e:
            host.logger.error(f"[审批] 失败（{via}）: {e} group={group_openid[-6:]}")
            return False, str(e)
        # 从待审批缓存移除，并清掉通知去重键——否则这条申请被处理掉之后，
        # 轮询仍可能因为去重表还没过期而反复提示。
        host._pending[group_openid] = [
            x for x in (host._pending.get(group_openid) or [])
            if host._req_key(x) != host._req_key(req)
        ]
        host._notified.get(group_openid, {}).pop(host._req_key(req), None)
        host.logger.info(
            f"[审批] 完成（{via}）: group={group_openid[-6:]} op={op} "
            f"member={member_openid[-6:]} reason={reason or '-'}"
        )
        return True, ""

async def _list_requests_all_groups(host, event, page):
        """按群汇总所有待处理申请（被动回复）。

        为什么要这个：群内通知受内联键盘行数、消息长度限制，一个群申请多了
        根本看不全；管理员想一次看完全部群的情况，只能翻面板。
        """
        groups = host._managed_groups()
        if not groups:
            await host._reply(event, host._render("msg_requests_empty"))
            return
        rows: list[str] = []
        total = 0
        for gid, pid in groups.items():
            try:
                items = await host._refresh_requests(gid, pid)
            except Exception as e:  # noqa: BLE001
                host.logger.debug(f"[审批] 汇总时拉取群 {gid} 失败: {e}")
                items = host._pending_list(gid)
            if not items:
                continue
            name = host.mod.display_name(gid) or gid[-6:]
            total += len(items)
            rows.append(f"【{name}】{len(items)} 条")
            for i, r in enumerate(items, start=1):
                rows.append(f"  {i}. {host._request_brief(r)}")
            rows.append("  处理：/群管 通过 序号（在该群发）")
        if not rows:
            await host._reply(event, host._render("msg_requests_empty"))
            return
        per = 30
        pages = max(1, (len(rows) + per - 1) // per)
        page = min(max(1, page), pages)
        chunk = rows[(page - 1) * per: page * per]
        head = f"（全部群汇总：{len(groups)} 个群，共 {total} 条，第 {page}/{pages} 页）"
        body = "\n".join([head] + chunk)
        if page < pages:
            body += f"\n下一页：/群管 审批 全部 {page + 1}"
        await host._reply(event, host._render("msg_requests_list", list=body))

async def _refresh_requests(host, group_openid, platform_id):
        """拉取最新申请并更新缓存，返回当前待审批列表。"""
        # 注意：本方法内部会更新 host._pending[group_openid]，
        # 调用方若需对比"新申请"，必须先保存上一轮缓存再调用。
        # _get_api 也可能抛 QQApiError（平台实例不存在 / 未配置 appid），
        # 必须一起包在 try 里：否则汇总模式下「某一个群的机器人已下线」
        # 会让整个 /requests 接口 500，前端整页显示「加载失败」。
        try:
            api = host._get_api(platform_id)
            data = await api.get_join_request_list(group_openid, limit=20)
            items = data.get("list") or []
            host._pending[group_openid] = items[-50:]
            host._join_error_code[group_openid] = None
            return items
        except QQApiError as e:
            host._join_error_code[group_openid] = e.code
            if e.code == 11703:
                # 机器人不是群管理员：这是权限前置条件，不是 bug，
                # 轮询侧会退避，这里只留一条 warning。
                host.logger.warning(
                    f"群 {group_openid} 拉取入群申请失败: 机器人不是群管理员（11703），已暂停该群轮询"
                )
            else:
                host.logger.error(f"拉取入群申请失败: {e}")
            return host._pending_list(group_openid)

def _join_button_options(host):
        """(是否带按钮, 按钮权限 admin|everyone, 每条通知最多几个申请)。"""
        enabled = bool(host._read_config_value(
            ("approval", "notify_buttons"), default=True, legacy_keys=("notify_buttons",),
        ))
        perm = str(host._read_config_value(
            ("approval", "button_permission"), default="admin",
            legacy_keys=("button_permission",),
        ) or "admin").strip().lower()
        if perm not in ("admin", "everyone"):
            perm = "admin"
        try:
            limit = int(host._read_config_value(
                ("approval", "button_max_requests"), default=3,
                legacy_keys=("button_max_requests",),
            ) or 3)
        except (TypeError, ValueError):
            limit = 3
        return enabled, perm, max(1, min(limit, button_kit.MAX_ROWS))

def _request_brief(r):
        """申请的一行摘要（不含序号）：昵称 (来源 时间) 验证消息。"""
        name = r.get("username") or r.get("member_openid") or "?"
        t = fmt_rfc3339(r.get("apply_at", ""))
        source = "邀请" if r.get("apply_source") == "invited" else "主动"
        brief = f"{name} ({source} {t})"
        try:
            vi = r.get("verify_info") or {}
            if vi.get("method") == "verify_message" and vi.get("verify_message"):
                brief += f" 验证消息: {vi['verify_message']}"
        except Exception:  # noqa: BLE001
            pass
        return brief

async def _flush_join_notify(host, group_openid, platform_id):
        """等一小会儿把同批申请合并成一条通知发出。"""
        try:
            await asyncio.sleep(host.JOIN_NOTIFY_DEBOUNCE_SECONDS)
            batch = host._join_fresh.pop(group_openid, [])
            if not batch:
                return
            if not host._join_notify_enabled():
                host.logger.debug("[入群申请] 通知已关闭，合并批次丢弃")
                return
            host.logger.debug(f"[入群申请] 合并推送 {len(batch)} 条申请")
            await host._notify_join_requests(
                group_openid, platform_id, batch, source="事件"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            host.logger.error(f"[入群申请] 合并推送失败: {e}")
        finally:
            host._join_notify_tasks.pop(group_openid, None)

def _schedule_join_notify(host, group_openid, platform_id):
        """安排一次「合并推送」。已有待发任务时直接复用，不再叠加定时器。"""
        task = host._join_notify_tasks.get(group_openid)
        if task is not None and not task.done():
            return
        try:
            host._join_notify_tasks[group_openid] = asyncio.create_task(
                host._flush_join_notify(group_openid, platform_id)
            )
        except RuntimeError as e:
            # 没有事件循环（理论上不会发生，事件回调本身就在循环里）。
            # 批次会留在 _join_fresh 里，下一条事件到达时会再次触发合并推送。
            host.logger.warning(f"[入群申请] 无法创建合并任务（{e}），等下次事件重试")

def _claim_join_welcome(host, group_openid, member_openid):
        """抢占该成员本次入群的欢迎发送权（先写表、后发送）。

        审批通过（按钮/指令/轮询）和 GROUP_MEMBER_ADD 是两条独立流程，
        而且事件往往比审批流程的后续代码先跑完，所以"发送前检查、发送后
        记录"必然漏掉并发窗口。改成发送前先把时间戳写进表里：谁先占到谁发，
        另一条路径直接跳过。

        Returns:
            True 表示抢到（由本次调用负责发欢迎），False 表示窗口内已有人欢迎过。
        """
        now = time.time()
        key = f"{group_openid}:{member_openid}"
        last = float(host._join_welcomed.get(key) or 0)
        if last and now - last < host.JOIN_WELCOME_DEDUPE_SECONDS:
            return False
        host._join_welcomed[key] = now
        host._prune_join_welcomed(now)
        return True

def _prune_join_welcomed(host, now=None):
        """清理入群欢迎去重表：过期记录删掉，最多留 500 条。"""
        now = float(now if now is not None else time.time())
        keep = {
            k: v for k, v in host._join_welcomed.items()
            if now - float(v or 0) < host.JOIN_WELCOME_DEDUPE_SECONDS
        }
        if len(keep) > 500:
            for k, _ in sorted(keep.items(), key=lambda kv: kv[1])[: len(keep) - 500]:
                keep.pop(k, None)
        host._join_welcomed = keep

async def _welcome_after_approve(host, event, group_openid, platform_id, member_openid, username):
        """审批通过后发欢迎语（主动消息优先，推不上去就把文案交回调用方）。

        从 ``_cmd_review`` 里抽出来，让按钮路径也能复用同一套降级逻辑。

        Args:
            event: 来源事件，可为 None（按钮路径拿不到可回复的 msg_id，仅记日志）。

        Returns:
            主动消息推成功、欢迎语功能关闭、或文案为空时返回 None；
            主动消息推失败时返回欢迎语文案，**调用方必须把它附在自己的回复里发出去**
            —— 早期这里用 ``host._reply(event, ...)`` 兜底，但按钮事件的 event
            没有 msg_id，适配器会直接跳过发送，欢迎语就这么静默消失了。
        """
        if not bool(host._read_config_value(
            ("basic", "enable_welcome"), default=True, legacy_keys=("enable_welcome",)
        )):
            return None
        if not host._claim_join_welcome(group_openid, member_openid):
            # 成员真进群的 GROUP_MEMBER_ADD 已经先发过欢迎了，这里别再补一条
            host.logger.info(
                f"群 {group_openid[-6:]} 成员 "
                f"{member_registry_lib.short_id(member_openid)} 刚欢迎过，审批路径跳过欢迎语"
            )
            return None
        welcome = host._render(
            "msg_welcome_after_approve",
            username=username,
            group_name=host._welcome_group_name(group_openid),
            bot_name=platform_id,
            at=member_openid,
        )
        if await host._send_group_proactive(
            group_openid, welcome, platform_id, at_openid=member_openid
        ):
            host.logger.info(f"已向群 {group_openid[-6:]} 推送欢迎语")
            return None
        host.logger.warning(
            "主动推送欢迎语失败（主动消息受限或频控），改由调用方随回执一起发出"
        )
        text = str(welcome or "").strip()
        if not text:
            host._release_join_welcome(group_openid, member_openid)
            return None
        # 这段文案马上就要由调用方发出去（回执/指令回复），槽位保持占用：
        # 否则成员真正进群触发 GROUP_MEMBER_ADD 时会再欢迎一遍。
        return text
