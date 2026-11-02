"""群成员事件支持（运行时注入，不改 AstrBot / qq-botpy 任何文件）。

背景
----
此外还注册了 GROUP_ADD_ROBOT（机器人被拉进群）handler：该事件的 intent 与「群@消息」
同一个位（GROUP_AND_C2C_EVENT, 1<<25），本来就已经订阅，且 botpy 自带 parser，
因此只需要挂一个 handler，不需要改 intents。

原始说明：QQ 官方机器人网关会下发群成员事件（如 GROUP_MEMBER_REMOVE，即成员退群/被移出），
但要收到它需要同时满足三个条件：

1. Intents 打开 GROUP_MEMBER_EVENT 位（1 << 24）；
2. botpy 的 ConnectionState 实例里注册了对应事件的 parser；
3. botpy client 实例上存在 on_<event> 处理器。

而当前 qq-botpy 1.2.1 的 Intents 没有暴露这个位、也没有群成员事件的 parser，
AstrBot 的 qqofficial 适配器同样没有订阅。本模块在插件加载时对「运行中的
实例」做一次性注入：

- 往 ConnectionState.parsers 塞 group_member_remove 解析器（立即生效）；
- 往 client 实例挂 on_group_member_remove 处理器（立即生效）；
- 打开 client.intents 的 1<<24 位并更新会话 intent（下次 identify 生效）；
- 触发一次受控重连（清空 session_id 后走 identify），让服务端开始推送。

重连后 ConnectionState / client 实例均不重建，因此注入结果会一直保留到
AstrBot 重启；重启后插件 initialize 会再次注入，无需额外维护。
"""

import asyncio
import types

from astrbot.api import logger as _astrbot_logger

PLATFORM_NAME = "qq_official"  # 平台名（meta().name），非实例 ID
GROUP_MEMBER_EVENT_INTENT = 1 << 24  # QQ 官方: GROUP_MEMBER_EVENT

def _install_gateway_sniffer(logger) -> None:
    """在 botpy 的收包入口挂一个探针，记录网关到底推了哪些事件。

    排障用：退群/入群通知不生效时，先分清是「平台根本没推」还是「推了但没接住」。
    只对 member / robot 类事件打 INFO，其余降到 DEBUG，避免刷屏。
    """
    try:
        from botpy.gateway import BotWebSocket
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[成员事件] 网关探针安装失败（botpy 不可用）: {e}")
        return
    if getattr(BotWebSocket, "_qgm_sniffed", False):
        return

    original = BotWebSocket.on_message

    async def _sniffing_on_message(self, ws, message):
        try:
            import json as _json

            if isinstance(message, (str, bytes, bytearray)):
                msg = _json.loads(message)
                t = str(msg.get("t") or "")
                if t and t not in ("READY", "RESUMED"):
                    low = t.lower()
                    if "member" in low or "robot" in low:
                        logger.info(f"[成员事件][网关] 收到事件 {t}")
                    else:
                        logger.debug(f"[成员事件][网关] 收到事件 {t}")
        except Exception:  # noqa: BLE001
            pass
        return await original(self, ws, message)

    BotWebSocket.on_message = _sniffing_on_message
    BotWebSocket._qgm_sniffed = True

    if not getattr(BotWebSocket, "_qgm_identify_logged", False):
        original_identify = BotWebSocket.ws_identify

        async def _logging_ws_identify(self):
            intent = 0
            try:
                intent = int((getattr(self, "_session", None) or {}).get("intent", 0))
            except Exception:  # noqa: BLE001
                pass
            if intent & GROUP_MEMBER_EVENT_INTENT:
                logger.info(f"[成员事件][网关] identify 上报 intents=0x{intent:x}（含群成员事件）")
            else:
                logger.warning(
                    f"[成员事件][网关] identify 上报 intents=0x{intent:x}（不含群成员事件，"
                    f"服务端不会推成员事件）"
                )
            return await original_identify(self)

        BotWebSocket.ws_identify = _logging_ws_identify
        BotWebSocket._qgm_identify_logged = True
    logger.info("[成员事件] 网关探针已安装：group/member 类事件会记录（用于确认平台是否推送）")


def install_member_event_support(plugin, context) -> int:
    """向所有 qq_official 适配器实例注入群成员事件支持。

    注意：AstrBot 支持多实例（同一平台可配多个机器人），get_platform_inst()
    按「实例 ID」（meta().id）匹配，而 qq_official 只是「平台名」（meta().name），
    直接传 "qq_official" 永远匹配不到实例。因此这里遍历 platform_manager
    中所有平台名为 qq_official 的实例，逐个注入；事件到达后按群路由到
    对应实例的 API 客户端（见 main._api_for_group_or_default）。

    Args:
        plugin: 群管插件实例（需提供 logger 属性）。
        context: AstrBot Star Context。

    Returns:
        成功注入的实例数量（0 表示无可用 qq_official 实例）。
    """
    logger = getattr(plugin, "logger", _astrbot_logger)
    insts = []
    try:
        for platform in context.platform_manager.platform_insts:
            try:
                if platform.meta().name == PLATFORM_NAME:
                    insts.append(platform)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[成员事件] 遍历平台实例失败: {e}")
        return 0
    if not insts:
        logger.info(
            f"[成员事件] 未找到平台名为 {PLATFORM_NAME} 的适配器实例，"
            f"跳过群成员事件注入（当前平台实例: "
            + ",".join(
                getattr(p.meta(), "name", "?") for p in context.platform_manager.platform_insts
            )
            + "）"
        )
        return 0

    injected = 0
    for idx, platform in enumerate(insts):
        pid = getattr(platform.meta(), "id", None) or PLATFORM_NAME
        client = getattr(platform, "client", None)
        connection = getattr(client, "_connection", None)
        state = getattr(connection, "state", None)
        if client is None or state is None:
            logger.warning(
                f"[成员事件] 实例 {pid} 适配器内部结构不符合预期，跳过注入"
            )
            continue

        # 0) 入群事件 GROUP_ADD_ROBOT：botpy 自带 parser（parse_group_add_robot），
        #    且 intent 位 1<<25 与群@消息共用、早就在订阅，所以挂个 handler 就够了。
        async def on_group_add_robot(data, _pid=pid):
            try:
                await plugin._on_group_add_robot(data, _pid)
            except Exception:
                logger.exception("[入群事件] 处理异常")

        client.on_group_add_robot = on_group_add_robot
        logger.info(f"[入群事件] 实例 {pid}: 已注册 GROUP_ADD_ROBOT handler")

        # 1) parser：GROUP_MEMBER_REMOVE 事件体 -> 派发给 client.on_group_member_remove
        def parse_group_member_remove(self, payload):
            d = payload.get("d", {}) if isinstance(payload, dict) else {}
            # 事件最外层的 id 是 event_id（被动消息要用）；事件体里没有，这里补进去
            if isinstance(d, dict):
                d = dict(d)
                d.setdefault("_event_id", payload.get("id", "") if isinstance(payload, dict) else "")
            logger.info(
                f"[成员事件] 收到 GROUP_MEMBER_REMOVE（实例 {pid}）"
                f"group={str(d.get('group_openid') or '')[-6:]} "
                f"member={str(d.get('member_openid') or '')[-6:]}"
            )
            self._dispatch("group_member_remove", d)

        state.parsers["group_member_remove"] = types.MethodType(
            parse_group_member_remove, state
        )

        # 1b) 同一位 intent 下的 GROUP_MEMBER_ADD（成员入群），一并接上：
        #     入群瞬间就能建档案（只有 openid），退群时"在群多久"才有据可查。
        def parse_group_member_add(self, payload):
            d = payload.get("d", {}) if isinstance(payload, dict) else {}
            if isinstance(d, dict):
                d = dict(d)
                d.setdefault("_event_id", payload.get("id", "") if isinstance(payload, dict) else "")
            logger.info(
                f"[成员事件] 收到 GROUP_MEMBER_ADD（实例 {pid}）"
                f"group={str(d.get('group_openid') or '')[-6:]} "
                f"member={str(d.get('member_openid') or '')[-6:]}"
            )
            self._dispatch("group_member_add", d)

        state.parsers["group_member_add"] = types.MethodType(parse_group_member_add, state)

        # 2) client handler：转发给插件处理（闭包带上实例 ID，便于按群路由）
        async def on_group_member_remove(data, _pid=pid):
            try:
                await plugin._on_group_member_remove(data, _pid)
            except Exception:
                logger.exception("[成员事件] 处理退群事件异常")

        client.on_group_member_remove = on_group_member_remove

        # 2b) 入群 handler
        async def on_group_member_add(data, _pid=pid):
            try:
                await plugin._on_group_member_add(data, _pid)
            except Exception:
                logger.exception("[成员事件] 处理入群事件异常")

        client.on_group_member_add = on_group_member_add

        # 3) 打开 GROUP_MEMBER_EVENT 位
        #    注意：identify 发的是「每个 websocket 自己的 session["intent"]」，
        #    client.intents 只是蓝图。只改蓝图不改真实 session，服务端永远不知道我们要订阅，
        #    事件自然一条都不会推 —— 所以这里逐个改活动连接上的 session。
        old_intents = client.intents
        client.intents |= GROUP_MEMBER_EVENT_INTENT

        sessions = []
        for ws in list(getattr(client, "_active_websockets", set()) or []):
            sess = getattr(ws, "_session", None)
            if isinstance(sess, dict) and all(sess is not x for x in sessions):
                sessions.append(sess)
        for sess in getattr(connection, "_session_list", []) or []:
            if isinstance(sess, dict) and all(sess is not x for x in sessions):
                sessions.append(sess)

        missing = [x for x in sessions if not (x.get("intent", 0) & GROUP_MEMBER_EVENT_INTENT)]
        for sess in sessions:
            sess["intent"] = client.intents
        changed = bool(missing) or bool(old_intents != client.intents)

        if sessions:
            shown = ",".join(f"0x{x.get('intent', 0):x}" for x in sessions)
            logger.info(
                f"[成员事件] 实例 {pid}: 当前连接 intents={shown}"
                f"（GROUP_MEMBER_EVENT "
                f"{'已含' if all(x.get('intent', 0) & GROUP_MEMBER_EVENT_INTENT for x in sessions) else '缺失'}）"
            )
        else:
            logger.warning(f"[成员事件] 实例 {pid}: 没找到活动连接 session，无法确认订阅状态")

        # 4) 触发受控重连，让 identify 带上新 intents（多实例错开延迟，避免同时断连）
        if changed:
            loop = asyncio.get_event_loop()
            loop.create_task(_reconnect_after(plugin, client, delay=8 + idx * 5))
            logger.info(
                f"[成员事件] 实例 {pid}: 已打开 GROUP_MEMBER_EVENT 订阅，"
                f"{8 + idx * 5} 秒后触发 websocket 重连生效"
            )
        else:
            logger.info(f"[成员事件] 实例 {pid}: GROUP_MEMBER_EVENT 位已开启，无需重连")

        injected += 1
        for s in getattr(connection, "_session_list", []) or []:
            if isinstance(s, dict):
                logger.debug(
                    f"[成员事件] 实例 {pid}: client.intents=0x{client.intents:x} "
                    f"(GROUP_MEMBER_EVENT={bool(client.intents & GROUP_MEMBER_EVENT_INTENT)}), "
                    f"session intent=0x{s.get('intent', 0):x}"
                )

    logger.info(
        f"[成员事件] 群退群事件注入完成，共 {injected} 个 qq_official 实例"
        f"（parser + handler 已注册）"
    )
    _install_gateway_sniffer(logger)
    return injected


async def _reconnect_after(plugin, client, delay: float) -> None:
    """延迟后关闭当前 websocket，触发 botpy 走 identify（带新 intents）重连。

    AstrBot 的 ManagedBotWebSocket.close() 会把 _can_reconnect 置 False，
    这样 on_closed 时会清空 session_id，重连即重新 identify，新 intents 生效。
    """
    logger = getattr(plugin, "logger", _astrbot_logger)
    await asyncio.sleep(delay)
    wss = list(getattr(client, "_active_websockets", set()) or [])
    if not wss:
        logger.warning("[成员事件] 没有活动的 websocket，无法自动重连；请重启 AstrBot 生效")
        return
    for ws in wss:
        try:
            await ws.close()
        except Exception:
            pass
    logger.info("[成员事件] 已触发 websocket 重连，群成员事件订阅将在重连后生效")
