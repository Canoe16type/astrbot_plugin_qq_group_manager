"""日报定时发送。"""

from __future__ import annotations


async def maybe_send_report(host):
        """每 30 秒被叫一次：到点后把「有未处理申请的群」逐个汇报一遍，每天一轮。

        为什么按群发而不是发到一个汇总群：待处理申请是**各群自己的事**，
        汇报发到群里是给该群管理员看的（谁申请进这个群、这个群还压着几条），
        汇总到一个群既没意义也容易泄别的群的信息。
        """
        enabled, (hh, mm), target = host._report_options()
        if not enabled:
            return
        now = host._local_now()
        today = now.strftime("%Y-%m-%d")
        if host._report_last_day == today:
            return
        if (now.hour, now.minute) < (hh, mm):
            return
        groups = host._report_candidates(target)
        if not groups:
            host.logger.warning("[日报] 还没有任何已知群，今天跳过定时汇报")
            host._report_last_day = today  # 避免每 30 秒刷一次日志
            host._save_report_state()
            return

        sent = skipped = failed = 0
        for group_openid, platform_id in groups:
            try:
                pending = await host._refresh_requests(group_openid, platform_id)
            except Exception as e:  # noqa: BLE001
                host.logger.warning(
                    f"[日报] 拉取群 {group_openid[-6:]} 待处理申请失败，改用缓存: {e}"
                )
                pending = host._pending_list(group_openid)
            # 拉取失败时缓存可能是旧的，宁可这轮不发，也别推一条「假日报」
            err = host._join_error_code.get(group_openid)
            if err:
                host.logger.warning(
                    f"[日报] 群 {group_openid[-6:]} 申请列表不可用（code={err}），本轮跳过"
                )
                skipped += 1
                continue
            if not pending:
                skipped += 1
                continue
            text = await host._build_report_text(group_openid, platform_id, pending=pending)
            ok = await host._send_group_proactive(group_openid, text, platform_id)
            if ok:
                sent += 1
                host.logger.info(
                    f"[日报] 已发送到群 {group_openid[-6:]}"
                    f"（待处理 {len(pending)} 条，{now.strftime('%H:%M')}）"
                )
            else:
                failed += 1
                host.logger.warning(
                    f"[日报] 群 {group_openid[-6:]} 发送失败；"
                    "主动消息可能受权限/频控限制，可用 /群管 汇报 手动查看"
                )
        host._report_last_day = today
        host._save_report_state()
        host.logger.info(
            f"[日报] 本轮完成：发送 {sent} 群 / 无待处理跳过 {skipped} 群 / 失败 {failed} 群"
        )
