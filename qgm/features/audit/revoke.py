"""处罚撤销：查真实禁言状态 → 解禁 → 复查 → 群内致歉 → 留审计。

从 main.py 机械搬迁，业务规则与 1.6.0 一致。
"""

from __future__ import annotations

import time

try:
    from .... import moderation
except ImportError:  # 测试把插件目录直接加入 sys.path 时
    import moderation  # type: ignore


async def revoke_verdict(host, group: str, vid: int, *, operator: str = "",
                         operator_name: str = "", reason: str = "",
                         is_bot_admin: bool = False) -> dict:
        """撤销一条已生效的处罚：留审计 → 尝试解禁 → 从「前科统计」中排除。

        解禁以**接口真值**为准（get_mute_state），不信本地禁言记录：
        群管手动解禁、自然到期、机器人重启都会让本地记录漂移。
        """
        rec = host.mod.get_verdict(vid)
        if rec is None:
            return {"ok": False, "error": "not_found"}
        if str(rec.get("group") or "") != str(group):
            return {"ok": False, "error": "cross_group"}
        if rec.get("revoked"):
            return {"ok": True, "already": True, "record": rec}
        if not rec.get("violation") or not (
            rec.get("executed") or str(rec.get("action") or "") in ("warn", "recall", "mute")
        ):
            return {"ok": False, "error": "not_punished"}
        target = str(rec.get("target") or "")
        if not is_bot_admin and target and target == str(operator):
            return {"ok": False, "error": "self_revoke"}

        unmute = {"needed": False, "ok": None, "code": ""}
        muted = bool(rec.get("ban_seconds")) or str(rec.get("action") or "") == "mute"
        if target and muted:
            unmute["needed"] = True
            try:
                api = host._api_for_group_or_default(group)
                state = await api.get_mute_state(group)
                members = state.get("members") or []
                still = any(str(m.get("member_openid") or "") == target for m in members)
                if still:
                    ok, err = await host._run_action(api.set_mute(group, target, op="del"))
                    if not ok:
                        raise RuntimeError(err)
                    host._mark_unmuted(group, target)
                    try:                                     # 复查，别"看起来成功了"
                        state2 = await api.get_mute_state(group)
                        still = any(str(m.get("member_openid") or "") == target
                                    for m in (state2.get("members") or []))
                    except Exception:
                        still = False
                    unmute["ok"] = not still
                else:
                    unmute["ok"] = True                      # 本来就已经不在了（手动/到期）
            except Exception as e:
                unmute["ok"] = False
                unmute["code"] = str(e)[:120]

        info = {
            "by": str(operator or ""),
            "by_name": str(operator_name or "")[:40],
            "at": int(time.time()),
            "reason": str(reason or "")[:200],
            "unmute_needed": unmute["needed"],
            "unmute_ok": unmute["ok"],
            "unmute_code": unmute["code"],
        }
        host.mod.update_verdict(vid, revoked=info)
        host.logger.info(
            f"{host._ctx('', group, record=vid)}[审核] 撤销处罚 by={info['by_name'] or info['by']} "
            f"reason={info['reason']!r} 解禁={unmute}"
        )
        return {"ok": True, "record": rec, "revoke": info, "unmute": unmute}


async def cmd_revoke(host, event, parts: list[str]) -> None:
        """撤销处罚：/群管 撤销 @用户 [理由]｜/群管 撤销 <记录ID> [理由]｜/群管 撤销 列表 [@用户] [条数]"""
        g = event.get_group_id()
        if not host._is_admin(event):
            await host._reply(event, "只有群主、群管理员或机器人管理员可以撤销处罚或查看记录。")
            return
        args = [str(x) for x in parts[1:]]
        is_bot_admin = host._is_bot_admin_only(event)
        targets = host._mentioned_openids(event)

        # ---- 列表模式：查这个人的判定/处罚历史（撤销前先看）----
        if args and args[0] in ("列表", "记录", "历史"):
            limit = 5
            for a in args[1:]:
                if a.isdigit():
                    limit = max(1, min(20, int(a)))
                    break
            recs = []
            for v in host.mod.list_verdicts(g, limit=400):
                if targets and str(v.get("target") or "") not in targets:
                    continue
                recs.append(v)
                if len(recs) >= limit:
                    break
            if not recs:
                await host._reply(event, "没有查到判定记录。可以带条数，例如：/群管 撤销 列表 @某人 10")
                return
            who = f"@{recs[0].get('target_name') or recs[0].get('target')}" if targets else "本群最近"
            lines = [f"【判定记录】{who}（最近 {len(recs)} 条）"]
            lines += [host._fmt_verdict_line(v) for v in recs]
            lines.append("撤销：/群管 撤销 @某人 [理由]，或 /群管 撤销 <记录ID> [理由]")
            await host._reply(event, "\n".join(lines))
            return

        # ---- 指定记录 ID ----
        vid, reason = 0, ""
        for i, a in enumerate(args):
            if a.isdigit():
                vid = int(a)
                reason = " ".join(args[i + 1:])
                break

        # ---- 未给 ID：按 @用户 找最近一条可撤销的处罚 ----
        if not vid:
            target = targets[0] if targets else ""
            if not target:
                await host._reply(event, "用法：\n"
                                          "· /群管 撤销 @某人 [理由]（撤销他最近一次处罚）\n"
                                          "· /群管 撤销 <记录ID> [理由]\n"
                                          "· /群管 撤销 列表 @某人 [条数]（先看判定历史）")
                return
            rec = next((v for v in host.mod.list_verdicts(g, limit=400)
                        if str(v.get("target") or "") == target and host._is_punishable(v)), None)
            if rec is None:
                await host._reply(event, "这个成员没有可撤销的处罚记录。可以先看历史：/群管 撤销 列表 @某人")
                return
            vid = int(rec.get("id") or 0)
            reason = " ".join(a for a in args if not a.isdigit() and not a.startswith("@") and not a.startswith("<@"))

        rec = host.mod.get_verdict(vid)
        if rec is None:
            await host._reply(event, f"没找到记录 #{vid}。")
            return
        if str(rec.get("group") or "") != str(g):
            await host._reply(event, "只能撤销本群的处罚记录。")
            return

        res = await host._revoke_verdict(
            g, vid,
            operator=event.get_sender_id() or "",
            operator_name=event.get_sender_name() or "",
            reason=reason,
            is_bot_admin=is_bot_admin,
        )
        if not res.get("ok"):
            reason_map = {
                "self_revoke": "不能撤销针对自己的处罚（机器人管理员除外）。",
                "not_punished": "这条记录没有实际处罚（未违规 / 试运行 / 未执行），无需撤销。",
                "cross_group": "只能撤销本群的处罚记录。",
                "not_found": f"没找到记录 #{vid}。",
            }
            await host._reply(event, reason_map.get(res.get("error"), f"撤销失败：{res.get('error')}"))
            return
        if res.get("already"):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(res["record"]["revoked"].get("at") or 0)))
            await host._reply(event, f"记录 #{vid} 已经在 {ts} 被撤销过了。")
            return

        target = str(rec.get("target") or "")
        unmute = res.get("unmute") or {}
        if unmute.get("needed"):
            unmute_txt = "已解除" if unmute.get("ok") else (
                "无需解除(当前不在禁言中)" if unmute.get("ok") is True else "解除失败，请手动解禁"
            )
        else:
            unmute_txt = "无需解除"
        await host._reply(event, host._mod_tpl(
            g, "msg_mod_revoke",
            vid=vid,
            at=target,
            punish=host._punish_text(rec),
            unmute=unmute_txt + (f"（{unmute.get('code')}）" if unmute.get("code") else ""),
            operator=event.get_sender_name() or event.get_sender_id(),
            reason_line=(f"\n说明：{reason}" if reason else ""),
        ))
