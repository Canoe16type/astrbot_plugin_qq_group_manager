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


async def _cmd_list_requests(host, event, parts=None):
        """待处理入群申请：本群 / 全部群，可翻页。

        用法：
            /群管 审批            本群第 1 页
            /群管 审批 2          本群第 2 页
            /群管 审批 全部       所有已知群汇总
            /群管 审批 全部 2     汇总的第 2 页
        """
        g = event.get_group_id()
        args = [a for a in (parts or [])[1:]]
        all_groups = any(a in ("全部", "全群", "所有", "all") for a in args)
        page = 1
        for a in args:
            if a.isdigit():
                page = max(1, int(a))
        if all_groups:
            await host._list_requests_all_groups(event, page)
            return

        pending = host._pending_list(g)
        if not pending:
            pending = await host._refresh_requests(g, event.get_platform_id())
        if not pending:
            await host._reply(event, host._render("msg_requests_empty"))
            return
        per = host.REQUEST_PAGE_SIZE
        total = len(pending)
        pages = max(1, (total + per - 1) // per)
        page = min(page, pages)
        chunk = pending[(page - 1) * per: page * per]
        lines = [f"（本群 {total} 条，第 {page}/{pages} 页）"]
        for i, r in enumerate(chunk, start=(page - 1) * per + 1):
            # 序号用全局序号，保证 /群管 通过 N 与本列表一一对应
            lines.append(host._fmt_request_line(i, r))
        if page < pages:
            lines.append(f"下一页：/群管 审批 {page + 1}")
        await host._reply(event, host._render("msg_requests_list", list="\n".join(lines)))
