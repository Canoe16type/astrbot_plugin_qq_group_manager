"""审批指令实现：通过 / 拒绝 / 列表。"""

from __future__ import annotations


async def _cmd_review(host, event, parts):
        cmd = parts[0] if parts else "/通过"
        g = event.get_group_id()
        pending = host._pending_list(g)
        if not pending:
            pending = await host._refresh_requests(g, event.get_platform_id())
        if not pending:
            await host._reply(event, host._render("msg_requests_empty"))
            return
        if len(parts) < 2:
            await host._reply(event, host._render("msg_request_index_invalid"))
            return
        arg = parts[1]
        # 序号 或 昵称关键字
        idx: Optional[int] = None
        if arg.isdigit():
            idx = int(arg)
            if not (1 <= idx <= len(pending)):
                await host._reply(event, host._render("msg_request_index_invalid"))
                return
        else:
            for i, r in enumerate(pending, start=1):
                if arg.lower() in str(r.get("username", "")).lower():
                    idx = i
                    break
            if idx is None:
                await host._reply(event, host._render("msg_request_index_invalid"))
                return

        target = pending[idx - 1]
        member_openid = target.get("member_openid", "")
        username = target.get("username", member_openid)
        reason = " ".join(parts[2:]) if len(parts) > 2 else None
        op = "approve" if cmd == "/通过" else "decline"

        ok, err = await host._apply_join_decision(
            event, g, event.get_platform_id(), target, op, reason=reason, via="指令"
        )
        if not ok:
            await host._reply(event, host._render("msg_approve_failed", error=err))
            return
        if op == "approve":
            await host._reply(
                event, host._render("msg_approve_success", username=username)
            )
            welcome_left = await host._welcome_after_approve(
                event, g, event.get_platform_id(), member_openid, username
            )
            if welcome_left:
                await host._reply(event, welcome_left)
        else:
            await host._reply(
                event, host._render("msg_reject_success", username=username)
            )
