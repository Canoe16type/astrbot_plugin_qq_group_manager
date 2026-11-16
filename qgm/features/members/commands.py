"""成员相关指令实现。"""

from __future__ import annotations

from ...kernel.text import _purge_summary_text

try:
    from .... import member_registry as member_registry_lib
except ImportError:
    import member_registry as member_registry_lib  # type: ignore
try:
    from ....qq_api import QQApiError
except ImportError:
    from qq_api import QQApiError  # type: ignore


async def _cmd_member(host, event, parts):
        """群成员档案：列表 / 改名 / 备注 / 删除 / 清理 / 拉取 / 查。

        官方事件与消息里只有 member_openid、没有昵称，所以这张表得靠人工维护；
        面板「成员档案」页签改的是同一份数据（members.json）。
        """
        g = event.get_group_id()
        if not g:
            await host._reply(event, "这个指令要在群里发哈。")
            return
        if not host._members_on():
            await host._reply(event, "群成员档案已在配置里关闭（member_registry.enabled）。")
            return

        sub = parts[1].lower() if len(parts) > 1 else ""
        args = parts[2:]

        # 宽松一点：/群管 成员 2、/群管 成员 已退 都当作列表查询
        if sub.isdigit() or sub in ("已退", "退群", "在群", "全部", "无名", "未命名", "有名", "named"):
            args = [sub] + args
            sub = "列表"

        if sub in ("", "列表", "list"):
            status = "all"
            page = 1
            for a in args:
                if a in ("在群", "active"):
                    status = "active"
                elif a in ("已退", "退群", "left"):
                    status = "left"
                elif a in ("无名", "未命名", "unnamed"):
                    status = "unnamed"
                elif a in ("有名", "named"):
                    status = "named"
                elif a.isdigit():
                    page = max(1, int(a))
            await host._reply(event, host._render_member_list(g, status=status, page=page))
            return

        if sub in ("查", "搜索", "search"):
            kw = " ".join(args).strip()
            if not kw:
                await host._reply(event, "用法：/群管 成员 查 <昵称/备注/openid 片段>")
                return
            await host._reply(event, host._render_member_list(g, keyword=kw, title=f"搜索「{kw}」"))
            return

        if sub in ("改名", "设置", "rename", "set"):
            if len(args) < 2:
                await host._reply(event, "用法：/群管 成员 改名 <序号或openid> <昵称>\n先发 /群管 成员 列表 看序号。")
                return
            openid = host._member_ref(g, args[0])
            if not openid:
                await host._reply(event, f"没找到成员「{args[0]}」，先发 /群管 成员 列表 看看序号。")
                return
            name = " ".join(args[1:]).strip()[:40]
            host.members.set_name(g, openid, name, source="manual")
            await host._reply(event, f"✅ 已记下：{member_registry_lib.short_id(openid)} → {name or '（清空）'}")
            return

        if sub in ("备注", "note"):
            if len(args) < 1:
                await host._reply(event, "用法：/群管 成员 备注 <序号或openid> [备注内容]（留空即清除）")
                return
            openid = host._member_ref(g, args[0])
            if not openid:
                await host._reply(event, f"没找到成员「{args[0]}」。")
                return
            note = " ".join(args[1:]).strip()[:200]
            host.members.set_note(g, openid, note)
            await host._reply(event, f"✅ 备注已更新：{member_registry_lib.short_id(openid)} → {note or '（空）'}")
            return

        if sub in ("删除", "删", "delete", "del"):
            if not args:
                await host._reply(event, "用法：/群管 成员 删除 <序号或openid>")
                return
            openid = host._member_ref(g, args[0])
            if not openid:
                await host._reply(event, f"没找到成员「{args[0]}」。")
                return
            host.members.delete(g, openid)
            await host._reply(event, f"🗑 已从档案删除：{member_registry_lib.short_id(openid)}")
            return

        if sub in ("清理", "clean"):
            n = host.members.delete_left(g)
            await host._reply(event, f"🧹 已清理 {n} 条「已退群」档案。" if n else "没有「已退群」的档案需要清理。")
            return

        if sub in ("拉取", "同步", "sync"):
            await host._reply(event, await host._cmd_member_sync(g))
            return

        await host._reply(
            event,
            "群成员档案用法：\n"
            "/群管 成员 —— 看档案（默认按最近发言排序）\n"
            "/群管 成员 已退 / 无名 / 全部 —— 按状态筛\n"
            "/群管 成员 查 <关键词> —— 搜昵称/备注/openid\n"
            "/群管 成员 改名 <序号> <昵称> —— 人工补名字\n"
            "/群管 成员 备注 <序号> <备注>\n"
            "/群管 成员 删除 <序号> —— 删单条\n"
            "/群管 成员 清理 —— 清掉所有已退群记录\n"
            "/群管 成员 拉取 —— 调官方群成员接口补昵称（需白名单权限）\n"
            "改昵称也可以走网页面板「成员档案」页签。",
        )

async def _cmd_member_sync(host, group_openid):
        """尝试用官方群成员接口补昵称。

        注意：GET /v2/groups/{openid}/members 与 .../members/{openid}
        目前都是**内邀白名单**能力，普通机器人调用返回 11253。这里如实回报，
        不做"假装成功"的提示。
        """
        api = host._api_for_group_or_default(group_openid)
        if api is None:
            return "没有可用的机器人实例，先确认 QQ 官方适配器在线。"
        try:
            members = await api.list_group_members(group_openid)
        except QQApiError as e:
            if e.code == 11253:
                return (
                    "❌ 官方「群成员列表」接口还是内邀能力（错误码 11253），本机器人没有权限。\n"
                    "可以先用 /群管 成员 改名 手工补，或在面板「成员档案」页签里批量改。"
                )
            return f"❌ 拉取失败：{e}"
        except Exception as e:  # noqa: BLE001
            return f"❌ 拉取失败：{e}"
        if not members:
            return "接口没返回成员（可能权限不足或群为空）。"
        rows = [
            {
                "openid": m.get("member_openid") or "",
                "nickname": m.get("username") or "",
                "role": m.get("member_role") or "",
                "user_openid": m.get("union_openid") or m.get("user_openid") or "",
                "joined_at": m.get("joined_at") or "",
            }
            for m in members
        ]
        added, updated = host.members.upsert_many(group_openid, rows)
        return (
            f"✅ 已从官方接口同步 {len(rows)} 人（新增 {added}、更新 {updated}）。\n"
            f"人工改过的昵称不会被覆盖。"
        )

async def _cmd_presence(host, event, parts):
        """/群管 体检：逐个确认机器人还在不在已记录的群里。

        默认只报告；带 ``清理`` 才真删数据（避免一次手滑清一堆群）。
        bot_state 需要白名单权限，拿不到权限的群一律标「无法确认」，不动数据。
        """
        do_purge = any(str(p).lower() in ("清理", "purge", "clean") for p in parts[1:])
        cur = event.get_group_id()
        groups = dict(host._managed_groups())
        if cur and cur not in groups:
            groups[cur] = event.get_platform_id()

        if not groups:
            await host._reply(event, "还没有记录到任何群，等机器人在群里收到消息或收到入群事件后再说。")
            return

        lines = [f"【群管体检】已知群 {len(groups)} 个" + ("（检出离群会自动清理）" if do_purge else "（只报告，不改数据；加「清理」才会删）")]
        stat = {"in": 0, "absent": 0, "unknown": 0}
        purged = 0
        for g, pid in list(groups.items())[:20]:      # 上限 20 个，避免回执太长
            state, detail = await host._check_group_presence(g, pid)
            stat[state] = stat.get(state, 0) + 1
            label = host.mod.display_name(g) or g
            tail = f"…{g[-6:]}"
            if state == "in":
                lines.append(f"✅ {label}（{tail}）{detail}")
            elif state == "absent":
                if do_purge and host._purge_enabled():
                    summary = host._purge_group_data(g, pid, reason="/群管 体检 检出离群")
                    purged += 1
                    lines.append(f"❌ {label}（{tail}）{detail}，已清理：{_purge_summary_text(summary, empty='无残留数据')}")
                else:
                    lines.append(f"❌ {label}（{tail}）{detail}" + ("（加「清理」可删除本群数据）" if not do_purge else "（自动清理开关已关，未删除）"))
            else:
                lines.append(f"⚠️ {label}（{tail}）{detail}")
        if len(groups) > 20:
            lines.append(f"……还有 {len(groups) - 20} 个群没检查（一次最多 20 个，可分批用 /群管 成员 查看）")
        lines.append(
            f"小结：在群 {stat.get('in', 0)}｜离群 {stat.get('absent', 0)}｜无法确认 {stat.get('unknown', 0)}"
            + (f"｜已清理 {purged} 个群" if purged else "")
        )
        await host._reply(event, "\n".join(lines))

async def _cmd_group_name(host, event):
        """刷新当前群的群名（管理员）。

        群名接口 GET /v2/groups/{openid}/info 仅白名单机器人可用（11253），
        无权限时如实回报，并提示改用面板手动备注。
        """
        grp = event.get_group_id()
        if not grp:
            await host._reply(event, "这个指令要在群里发哈。")
            return
        pid = event.get_platform_id()
        try:
            api = host._get_api(pid)
        except QQApiError as e:
            await host._reply(event, f"没找到这个机器人的实例：{e}")
            return
        data, err = await api.fetch_group_info(grp)
        if data and data.get("group_name"):
            name = str(data["group_name"])[:60]
            host.mod.set_group_meta(grp, name=name, platform_id=pid,
                                    name_state="ok", name_error="")
            await host._reply(event, f"本群群名：{name}")
            return
        code = getattr(err, "code", None)
        msg = str(err) if err else "未知错误"
        host.mod.set_group_meta(grp, platform_id=pid,
                                name_state=("denied" if code == 11253 else "failed"),
                                name_error=msg[:200])
        if code == 11253:
            await host._reply(event, "取不到群名：官方「获取群信息」接口只对白名单机器人开放"
                                     "（错误码 11253，需在 QQ 开放平台申请「群基本信息」权限）。\n"
                                     "临时办法：打开管理面板 →「群规审核」→ 点「✎ 备注」给这个群起个名字。")
        else:
            await host._reply(event, f"拉取群名失败：{msg}")
