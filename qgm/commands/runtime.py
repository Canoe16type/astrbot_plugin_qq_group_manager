"""指令运行时：权限校验与分发执行。"""

from __future__ import annotations

import re
import time
import traceback

from ..kernel.constants import ADMIN_COMMANDS
from . import parse as command_parse, router as command_router


async def _handle_command(host, event, text, forced_cmd=None, skip=0):
        # QQ 官方消息中 @ 以 <@openid> 文本形式保留在 content 里（AstrBot 适配器
        # 只会清理机器人自身的 @）。这里统一剥离所有 <@...> / <@!...> 片段（替换
        # 为空格）再解析参数，免得 @ 文本混进理由之类的字段；目标成员不依赖这段
        # 文本，而是从 raw_message.mentions 读取。
        cmd, parts = command_parse.parse_command(text, forced_cmd=forced_cmd, skip=skip)
        if not cmd:
            return
        # 说明：用户侧矩阵是 `/群管 <子指令>`；forced_cmd 只做内部归一，
        # 例如 `/群管 禁言 名单` → cmd=/禁言, parts=[/禁言, 名单]。

        if cmd == "/群管":
            await host._reply(event, host._render("msg_help", bot_name=event.get_platform_id()))
            return

        # /举报：所有群成员都能用（举报的意义就在于此），不受管理员门槛限制
        if cmd == "/举报":
            await host._cmd_report_violation(event, parts)
            return

        # /群规：查看对所有人生效；增删改在 _cmd_rules 内部单独校验管理员
        if cmd == "/群规":
            await host._cmd_rules(event, parts)
            return

        if cmd not in ADMIN_COMMANDS:
            return  # 非本插件指令，不打扰

        # 群白名单检查
        if not host._target_group_allowed(event.get_group_id(), event.get_platform_id()):
            host.logger.debug(f"群 {event.get_group_id()} 不在管理名单，忽略")
            return

        # 管理员检查
        if not host._is_admin(event):
            host.logger.info(
                f"权限不足: sender={event.get_sender_id()} 尝试执行 {cmd}"
            )
            await host._reply(
                event,
                host._render(
                    "msg_not_admin",
                    username=event.get_sender_name() or event.get_sender_id(),
                ),
            )
            event.call_llm = True  # 阻止 LLM 介入
            return

        event.call_llm = True  # 阻止 LLM 介入（已处理）
        route = command_router.resolve_admin(cmd, parts)
        if route:
            handler_name, args = route
            await getattr(host, handler_name)(event, args)

async def _run_command(host, event, forced_cmd=None, skip=0):
        """指令执行入口：提取原始文本 -> 权限校验 -> 分发执行。

        Args:
            forced_cmd: 由 @filter.command 注册的 handler 传入的规范指令名
                （形如 "/禁言"）。面板里改过指令名/别名后，靠它保证内部路由
                到正确的处理逻辑；为 None 时按消息原文解析（兼容入口用）。
            skip: 额外丢弃消息文本前 N 个 token。指令组写法
                （"/群管 禁言 @某人 10分钟"）需要丢掉组名之后、子指令名，
                否则子指令名会被当成参数（比如被误认成"要禁言的人"）。
        """
        try:
            # 防重入：AstrBot 会对所有正则命中的 handler 逐个执行，
            # "/禁言历史" 会被 "/禁言" 与 "/禁言历史" 两个 handler 同时触发，
            # 这里按消息 ID 去重，保证同一条消息只处理一次。
            msg_id = str(getattr(event.message_obj, "message_id", "") or "")
            now = time.time()
            if msg_id and msg_id == host._last_cmd_msg_id and now - host._last_cmd_ts < 5:
                host.logger.debug(f"同一条消息重复触发，已忽略 (msg_id={msg_id})")
                return
            if msg_id:
                host._last_cmd_msg_id = msg_id
                host._last_cmd_ts = now

            host._ensure_poll_task()
            host._ensure_report_task()  # WebUI 刚打开日报开关时，靠这条路径自愈
            host._record_recent_msg(event)  # 引用撤回：原文匹配需要消息缓存
            await host._maybe_welcome_new_group(event)
            text = host._extract_text(event)
            host.logger.debug(f"群消息文本: {text!r}")
            if not text:
                return
            # 指令必须以 "/" 开头，或满足以下两个条件之一才处理：
            # 1) 消息 @ 了机器人（群内常见的不带 / 指令写法）；
            # 2) 首 token 是已知指令名。
            # 这样既兼容 README 所述"不带 / 也可以"，又避免普通聊天内容误触发。
            clean = re.sub(r"<@!?[0-9A-Za-z_-]+>", " ", text).strip()
            if not clean:
                return
            first = clean.split()[0]
            if not first.startswith("/"):
                if not host._is_mentioned_bot(event):
                    return
                if first not in ("禁言", "审批", "状态", "撤回", "群管", "举报", "群规", "审核",
                                 "管理登记", "成员", "帮助", "撤销"):
                    return
            await host._handle_command(event, text, forced_cmd=forced_cmd, skip=skip)
        except Exception as e:
            host.logger.error(f"{host._ctx(event.get_platform_id(), event.get_group_id())}处理群消息异常: {e}")

            host.logger.debug(traceback.format_exc())
