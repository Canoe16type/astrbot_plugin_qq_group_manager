"""主动消息发送（机械搬迁）。"""

from __future__ import annotations

import time

try:
    from .... import button_kit
except ImportError:
    import button_kit  # type: ignore
try:
    from ....qq_api import QQApiError
except ImportError:
    from qq_api import QQApiError  # type: ignore


async def _send_group_proactive(host, group_openid, text, platform_id, *, at_openid='', keyboard=None):
        """主动往群里发一条模板消息（入群欢迎这类「没有可回复事件」的场景）。

        为什么必须走主动消息：官方 ``event_id`` 被动回复只支持 3 个事件
        （INTERACTION_CREATE / GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE），
        GROUP_MEMBER_ADD、GROUP_JOIN_REQUEST 都不在白名单里，拿它们的事件 id
        去回会直接 40034027。
        代价是受主动消息频控（未认证 30 条/分钟、单群 20 条/分钟、每群每天
        1000 条），且用户可在 QQ 客户端关掉「允许主动发送」。

        模板格式为图片（image）时只发配文：入群瞬间没有可渲染图片的消息上下文，
        图片模板退化成文字，总比什么都不发好。

        Args:
            keyboard: 内联键盘（按钮）。按钮相关错误码（305007 键盘参数错误、
                40034029 行列超限、40034124 markdown 参数错误、22006 类型不匹配）
                出现时会**去掉键盘重发一次**——宁可少按钮，也别把通知丢了。

        Returns:
            True 表示接口调用成功。
        """
        key = getattr(text, "tpl_key", "") or ""
        fmt = host._tpl_format(key) if key else "text"
        content = host._expand_msg_modules(
            str(text), None, fmt,
            getattr(text, "tpl_target", "") or at_openid,
            platform_id=platform_id,
        )
        if not content.strip():
            host.logger.debug(f"[主动消息] 模板 {key or '?'} 展开后为空，跳过发送")
            return False
        api = host._api_for_group_or_default(group_openid, platform_id)
        if api is None:
            host.logger.warning(
                f"[主动消息] 群 {str(group_openid)[-6:]} 找不到可用 API 客户端，发送跳过"
            )
            return False
        # ---- 发送阶梯：按钮优先，逐级降级，正文最后兜底 ----
        # 为什么不「失败就丢按钮」：markdown 通道对 keyboard 的校验比文本通道严
        # （实测 group_id 含冒号就 40011021 整条拒发），一次失败直接丢按钮，等于
        # 把最值钱的功能丢了。所以按「损失从小到大」逐级试：
        #   ① markdown + 完整键盘（有互斥变灰）
        #   ② markdown + 去掉 group_id 的键盘（按钮还在，只是不互斥）
        #   ③ markdown 纯正文（保住排版）
        #   ④ 纯文本（保住内容）
        plans: list = []
        if keyboard:
            plans.append((True, keyboard))
            if button_kit.has_group_id(keyboard):
                stripped = button_kit.without_group_id(keyboard)
                if stripped:
                    plans.append((True, stripped))
            plans.append((True, None))
            plans.append((False, None))
        elif fmt == "markdown":
            plans.append((True, None))
            plans.append((False, None))
        else:
            plans.append((False, None))

        last_err = None
        for idx, (md, kb) in enumerate(plans):
            kwargs: dict = {"markdown": True} if md else {}
            if kb:
                kwargs["keyboard"] = kb
            try:
                await api.send_group_message(group_openid, content, **kwargs)
                if keyboard and kb is None:
                    host.logger.warning(
                        "[主动消息] 已降级为无按钮消息（正文送达，但群里没有"
                        "「通过/拒绝」按钮）；审批可用 /群管 审批 或面板操作"
                    )
                elif keyboard and kb is not keyboard:
                    host.logger.info(
                        "[主动消息] 去掉 group_id（按钮互斥分组）后重发成功，按钮可用"
                    )
                return True
            except QQApiError as e:
                last_err = e
                code = getattr(e, "code", None)
                if code in host.FATAL_SEND_CODES and keyboard:
                    # 权限 / 频控 / 不在群：换格式一样失败，别为了按钮白刷四档请求。
                    # 注意只在「带按钮」时短路：无按钮的 markdown 降级只有一两档，
                    # 且正文优先，宁可多试一次也别把欢迎语/通知丢了。
                    host.logger.warning(
                        f"[主动消息] 群 {str(group_openid)[-6:]} 发送失败: {e}；"
                        f"{host._explain_send_error(code)}"
                    )
                    return False
                if idx + 1 < len(plans):
                    host.logger.warning(
                        f"[主动消息] 第 {idx + 1} 档发送失败（code={code}），继续降级: {e}"
                    )
        if last_err is not None:
            host.logger.warning(
                f"[主动消息] 群 {str(group_openid)[-6:]} 发送失败（所有降级档位均失败）: "
                f"{last_err}；{host._explain_send_error(getattr(last_err, 'code', None))}"
            )
        return False
