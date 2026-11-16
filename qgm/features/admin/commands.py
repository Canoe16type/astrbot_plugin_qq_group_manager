"""管理登记指令。"""

from __future__ import annotations


async def _cmd_admin_register(host, event, parts=None):
        """审核提醒名单的管理员登记：/管理登记 [取消|列表]

        为什么要登记而不是自动识别：QQ 官方通道下群消息拿不到稳定的角色字段
        （_get_qq_role 恒为 None），而"谁在线、谁愿意兜底"本来就是群自己的事。
        所以由管理员主动发一条指令把自己写进本群名单，变体累计提醒只 @ 名单内的人；
        一个群没人登记，就不发提醒（不刷群、不浪费主动消息配额）。
        """
        event.call_llm = True
        g = event.get_group_id() or ""
        if not g:
            await host._reply(event, "本指令只能在群里使用")
            return
        if not host._mod_group_allowed(g):
            return
        sender = str(event.get_sender_id() or "")
        name = str(event.get_sender_name() or "")
        sub = parts[1].strip() if len(parts) > 1 else ""
        if sub in ("列表", "查看", "list"):
            admins = host.mod.get_admins(g)
            if not admins:
                await host._reply(event, host._mod_tpl(g, "mod_admin_list_empty"))
                return
            names = "、".join((a.get("name") or f"<@{a['openid']}>") for a in admins[:20])
            await host._reply(event, host._mod_tpl(g, "mod_admin_list",
                                                   count=len(admins), admins=names))
            return
        if sub in ("取消", "移除", "注销", "off"):
            ok = host.mod.unregister_admin(g, sender)
            cnt = len(host.mod.get_admins(g))
            key = "mod_admin_unregistered" if ok else "mod_admin_not_registered"
            await host._reply(event, host._mod_tpl(g, key, user=f"<@{sender}>", at=sender, count=cnt))
            return
        # 登记：能走到这里说明已经过了管理员门槛（AstrBot 管理员 / 配置名单 / 群角色任一）
        role = str(host._get_qq_role(event) or "")
        if not role and host._is_qq_admin_openid(g, sender):
            role = "qq_admin"
        if not role:
            role = "astrbot_admin"
        host.mod.register_admin(g, sender, name=name, role=role, by=sender)
        cnt = len(host.mod.get_admins(g))
        host.logger.info(f"[审核] 管理员登记：group={g} sender={sender} role={role} 名单共 {cnt} 人")
        await host._reply(event, host._mod_tpl(g, "mod_admin_registered",
                                               user=f"<@{sender}>", at=sender, count=cnt))
        if not host._mod_settings(g).get("variant_strike_notify", True):
            await host._reply(event, host._mod_tpl(g, "mod_admin_register_off"))
