"""审批/惩罚按钮交互。"""

from __future__ import annotations

import time

try:
    from .... import button_kit
except ImportError:
    import button_kit  # type: ignore


async def _handle_button_interaction(host, event):
        """处理消息按钮点击，返回 True 表示这条事件已被本插件消费。"""
        bi = host._button_interaction_of(event)
        if bi is None:
            return False
        data = str(getattr(bi, "data", "") or "")
        if not data.startswith(button_kit.CALLBACK_PREFIX):
            return False  # 其他插件/其他用途的按钮，不插手

        iid = str(getattr(bi, "interaction_id", "") or "")
        now = time.time()
        # 幂等：同一 interaction 只处理一次（用户连点会重复推事件）
        host._interaction_done = {
            k: v for k, v in host._interaction_done.items() if now - v < 3600
        }
        if iid and iid in host._interaction_done:
            host.logger.debug(f"[按钮] interaction {iid[:8]}… 重复点击，忽略")
            return True
        if iid:
            host._interaction_done[iid] = now
        host._interaction_total += 1
        host._interaction_last = now

        group_openid = str(event.get_group_id() or "")
        operator = str(event.get_sender_id() or "")
        platform_id = event.get_platform_id()
        action = button_kit.parse_join_action(data)
        if action is None or not group_openid:
            host._interaction_last_result = "数据无效"
            host.logger.warning(
                f"[按钮] 回调数据无法解析或不在群场景（可能被伪造）：{data[:60]}"
            )
            await host._reply_button_result(
                event, group_openid, platform_id,
                host._render("msg_join_request_button_gone"),
            )
            return True

        if not host._group_in_scope(group_openid, platform_id):
            # 通知只发在生效范围内的群；配置被改过之后仍可能点到旧消息上的按钮，
            # 这里也要挡一道，避免越权处理。
            host._interaction_last_result = "群不在生效范围"
            host.logger.warning(
                f"[按钮] 群 {group_openid[-6:]} 不在生效范围内，忽略本次点击"
            )
            return True

        _on, perm, _limit = host._join_button_options()
        if not host._button_operator_allowed(operator, perm):
            host._interaction_last_result = "无权限"
            host.logger.warning(
                f"[按钮] 操作者 {operator[-6:]} 不在管理员名单，拒绝其审批操作"
            )
            await host._reply_button_result(
                event, group_openid, platform_id,
                host._render("msg_join_request_button_denied"),
            )
            return True

        # 真值以本地待审批列表为准：button_data 是不可信输入，只当「意图」。
        key = (str(action["member_openid"]), str(action["apply_at"]))
        pending = host._pending.get(group_openid) or []
        target = next((x for x in pending if host._req_key(x) == key), None)
        if target is None:
            # 缓存里没有：可能刚被指令处理掉，也可能缓存丢了 —— 主动拉一次对账
            try:
                fresh = await host._refresh_requests(group_openid, platform_id)
            except Exception:
                fresh = []
            target = next(
                (x for x in (fresh or []) if host._req_key(x) == key), None
            )
        if target is None:
            host._interaction_last_result = "已处理/已失效"
            host.logger.info(
                f"[按钮] 申请已不在待审批列表（member={key[0][-6:]} apply_at={key[1]}），"
                f"回执「已处理或已失效」"
            )
            await host._reply_button_result(
                event, group_openid, platform_id,
                host._render("msg_join_request_button_gone"),
            )
            return True

        op = "approve" if action["op"] == button_kit.OP_APPROVE else "decline"
        username = str(target.get("username") or key[0][-6:])
        ok, err = await host._apply_join_decision(
            event, group_openid, platform_id, target, op, via="按钮"
        )
        if not ok:
            host._interaction_last_result = f"失败: {err}"
            await host._reply_button_result(
                event, group_openid, platform_id,
                host._render("msg_join_request_button_failed", username=username, error=err),
            )
            return True

        if op == "approve":
            host._interaction_last_result = f"通过 {username}"
            # 先试一次主动推送欢迎语；推不上去（@ 的成员还没真正进群、或主动消息
            # 受限）就把它和回执合并，用 passive（event_id=互动事件 id）一次发出：
            # 这条通道不吃主动消息配额，也不依赖 msg_id，可靠得多。
            text = host._render("msg_join_request_button_ok", username=username)
            welcome_left = await host._welcome_after_approve(
                event, group_openid, platform_id,
                str(target.get("member_openid") or ""), username,
            )
            if welcome_left:
                text = f"{text}\n\n{welcome_left}"
            await host._reply_button_result(
                event, group_openid, platform_id, text
            )
        else:
            host._interaction_last_result = f"拒绝 {username}"
            await host._reply_button_result(
                event, group_openid, platform_id,
                host._render("msg_join_request_button_rejected", username=username),
            )
        return True
