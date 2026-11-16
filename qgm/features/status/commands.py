"""状态/自检/日报指令实现。"""

from __future__ import annotations

import platform

from ...kernel.constants import PLUGIN_VERSION


async def _cmd_status(host, event, parts=None):
        """/群管 状态 [日报|群|自检|群名]：所有「看一眼现状」的指令收在这一个入口。

        - 日报（默认，原 /群管 汇报）：待处理申请 + 当前禁言人数
        - 群（原 /群管 体检 [清理]）：机器人还在不在已记录的群里
        - 自检（原 /群管 自检）：审核链路配置与今日用量
        - 群名（原 /群管 群名）：刷新本群群名（面板群列表用）
        """
        sub = parts[1].lower() if len(parts) > 1 else ""
        args = parts[2:]
        if sub in ("", "日报", "汇报", "daily", "摘要", "summary"):
            await host._cmd_report(event)
        elif sub in ("群", "体检", "presence", "在群"):
            await host._cmd_presence(event, ["/状态"] + args)
        elif sub in ("自检", "诊断", "selfcheck", "diag"):
            await host._cmd_selfcheck(event)
        elif sub in ("群名", "名称", "name", "刷新群名"):
            await host._cmd_group_name(event)
        else:
            await host._reply(
                event,
                "用法：/群管 状态 [日报 | 群 [清理] | 自检 | 群名]\n"
                "· 日报＝待处理申请 + 禁言人数\n"
                "· 群＝机器人还在不在已记录的群里\n"
                "· 自检＝审核链路配置与今日用量\n"
                "· 群名＝刷新本群群名",
            )

async def _cmd_selfcheck(host, event, parts=None):
        """/群管 自检：一眼看清审核链路「配没配好、调没调上、花了多少」。

        刻意不做主动探活——探活等于真发一次请求，既花钱又要等；
        要看真调用的细节，把日志级别调到 DEBUG 看 [模型] 行即可。
        """
        g = event.get_group_id()
        st = host._mod_settings(g)
        try:
            pid = str(await host._mod_provider_id(event, group=g) or "")
        except Exception as e:                                  # provider 解析失败本身就是要报的问题
            pid = f"解析失败（{e}）"
        try:
            stats = host.mod.stats(g) or {}
        except Exception:
            stats = {}
        last = dict(host._mod_last_llm or {})
        budget = int(st.get("daily_budget") or 0)
        used = int(stats.get("today_llm_calls", 0) or 0)
        queue = int(st.get("llm_queue_limit") or 0)
        timeout = int(st.get("llm_timeout") or 0)
        if last:
            last_line = (f"最近一次模型调用：{last.get('scene') or '-'} "
                         f"{'成功' if last.get('ok') else '失败'} {last.get('ms', '?')}ms"
                         + (f"（{last.get('error')}）" if last.get("error") else ""))
        else:
            last_line = "最近一次模型调用：本进程内还没有（近期没走到模型层）"
        lines = [
            "【群管自检】",
            f"适配器：{event.get_platform_id() or '-'}",
            f"审核开关：{'开' if st.get('enabled') else '关'}"
            f"｜执行模式：{'试运行（只判定不执行）' if st.get('dry_run') else '真实执行'}",
            f"分层：关键词 {'开' if st.get('enable_keyword') else '关'}"
            f"／模型 {'开' if st.get('enable_model') else '关'}"
            f"／图片 {'开' if st.get('enable_vision') else '关'}",
            f"审核模型：{pid or '未配置（模型层会直接跳过，只跑关键词）'}",
            f"今日模型调用：{used}" + (f" / 预算 {budget}" if budget > 0 else "（未设上限）"),
            f"累计判定 {stats.get('total', 0)} 条"
            f"｜本次统计以来缓存命中率 {round(float(stats.get('cache_rate') or 0) * 100)}%",
            f"并发闸门：排队 {host._mod_llm_pending.get(g, 0)}/{queue or '不限'}"
            f"｜单次超时 {timeout or '不限'}s",
            last_line,
            f"运行环境：Python {platform.python_version()}｜插件 {PLUGIN_VERSION}",
            "提示：不做主动探活（会真花钱）；想看调用细节，把日志级别调到 DEBUG 看 [模型] 行。",
        ]
        await host._reply(event, "\n".join(lines))

async def _cmd_report(host, event, parts=None):
        """被动版日报：/群管 汇报 —— 立刻回一条，不等定时任务。

        和定时汇报共用 _build_report_text，区别只是「回复当前消息」而不是主动推送：
        主动消息要过频控、还依赖群未关闭主动消息，手动查的时候用被动回复更稳。
        """
        g = event.get_group_id()
        if not g:
            await host._reply(event, "这个指令要在群里发哈。")
            return
        text = await host._build_report_text(g, event.get_platform_id())
        await host._reply(event, text)
