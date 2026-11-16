"""审核执行与变体累积（原 main 服务方法，机械搬迁）。"""

from __future__ import annotations

import time

from ...kernel.timefmt import fmt_duration

try:
    from .... import moderation
except ImportError:  # 测试环境
    import moderation  # type: ignore
try:
    from ....qq_api import QQApi
except ImportError:
    from qq_api import QQApi  # type: ignore


async def _mod_execute(host, event, group, rep, verdict, settings):
        """按判定结果执行动作。返回执行摘要（含话术所需字段）。"""
        # 归因不可靠时一律只判定、不处罚：
        # - unattributed：引用超出缓存窗口，拿不到发送者
        # - match_ambiguous：有多条同文候选，选中哪条都可能是错的（禁错人的代价远大于漏判）
        forced_only = bool(rep.get("unattributed") or rep.get("match_ambiguous"))
        out = {
            "violation": bool(verdict.get("violation")),
            "basis": verdict.get("basis"),
            "category": verdict.get("category") or "other",
            "severity": str(verdict.get("severity") or ""),
            "confidence": verdict.get("confidence", 0),
            "reason": verdict.get("reason") or "",
            "rule_name": "",
            "seconds": 0,
            "duration": "",
            "executed": False,
            "skipped_reason": "",
            "recall_note": "",
            "dry_run": bool(settings.get("dry_run", True)) or forced_only,
            "unattributed": bool(rep.get("unattributed")),
            "ambiguous": bool(rep.get("match_ambiguous")),
            "cached": bool(verdict.get("from_cache")),
        }
        if not out["violation"]:
            return out
        plan = host._mod_plan(group, verdict, rep, settings=settings)
        out.update({
            "action": plan["action"],
            "rule_name": plan["rule_name"],
            "rule_id": plan["rule_id"],
            "match_by": plan["match_by"],
            "source": plan["source"],
            "severity": plan["severity"] or out["severity"],
            "seconds": plan["seconds"] if plan["action"] == "mute" else 0,
            "duration": plan["duration"] if plan["action"] == "mute" else "",
            "prior_offenses": plan["prior_offenses"],
            "escalation": plan["escalation"],
            "escalation_capped": bool(plan["escalation_capped"]),
            "policy_trace": plan.get("trace") or {},
        })
        kind = plan["action"]
        if kind == "none":
            out["skipped_reason"] = plan.get("reason") or "未触发处罚"
            host.logger.debug(
                f"{host._ctx(event.get_platform_id(), group)}[审核] 未处罚：{out['skipped_reason']}"
            )
            return out

        target = str(rep.get("sender") or "")
        if not target:
            out["skipped_reason"] = "无法确定被举报消息的发送者（消息过久未在缓存中）"
            return out
        # 豁免：管理员 / 白名单成员
        wl = [str(x) for x in (settings.get("whitelist") or [])]
        if target in wl or host._is_qq_admin_openid(group, target):
            out["skipped_reason"] = "该成员在豁免名单中"
            return out
        host.logger.info(
            f"{host._ctx(event.get_platform_id(), group, target)}[审核] 决策 action={kind} "
            f"时长={out.get('duration') or '-'} 前科={plan.get('prior_offenses')} "
            f"倍率×{plan.get('escalation')} 来源={plan.get('source')} 规则={plan.get('rule_name') or '-'}"
        )

        if out["dry_run"]:
            out["skipped_reason"] = "试运行模式，未实际执行"
            return out

        # 待办不需要任何 API 客户端，但如果计划要求「先撤回」，这里顺手把内容撤下来
        if kind == "todo":
            if plan.get("recall"):
                out["recall_note"] = await host._mod_try_recall(event, group, rep, settings)
            out["pending_review"] = True
            out["executed"] = False
            out["skipped_reason"] = out.get("skipped_reason") or "转人工复核"
            host.logger.info(f"{host._ctx(event.get_platform_id(), group, str(rep.get('sender') or ''))}[审核] 转人工待办：{out['skipped_reason']}")
            return out

        # 1) 撤回：警告与禁言都可能用到；失败不影响后续动作。
        # 官方限制：非管理员只能撤自己的消息（40062003）；超过 2 分钟不可撤回（40064004）。
        if kind in ("mute", "warn") and (kind == "mute" or plan.get("recall")):
            out["recall_note"] = await host._mod_try_recall(event, group, rep, settings)

        if kind == "warn":
            # 警告靠消息通道，不需要 API 客户端；发完即收口
            out["executed"] = True
            await host._send_mod_warning(event, group, rep, out, plan)
            host.logger.info(
                f"{host._ctx(event.get_platform_id(), group, target)}[审核] 已警告 severity={out.get('severity')} conf={out.get('confidence')}"
            )
            return out

        api = host._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
        if api is None:
            out["skipped_reason"] = "无可用 API 客户端"
            return out

        # 2) 禁言
        expire_ts = time.time() + int(out["seconds"])
        try:
            ok, err = await host._run_action(
                api.set_mute(group, target, mute_expire_at=QQApi.format_rfc3339(expire_ts), op="add")
            )
            if not ok:
                out["executed"] = False
                out["skipped_reason"] = err
                return out
        except Exception as e:
            host.logger.error(f"{host._ctx(event.get_platform_id(), group, target)}[审核] 禁言失败: {e}")
            out["skipped_reason"] = f"禁言失败：{e}"
            return out
        out["executed"] = True
        try:
            host._add_mute_record(
                group_openid=group,
                platform_id=event.get_platform_id(),
                member_openid=target,
                username=str(rep.get("sender_name") or ""),
                reason=f"举报审核：{out['reason']}"[:120],
                seconds=int(out["seconds"]),
                expire_ts=expire_ts,
                operator=event.get_sender_id(),
                operator_name=f"举报人:{event.get_sender_name() or ''}",
            )
        except Exception as e:
            host.logger.debug(f"{host._ctx(event.get_platform_id(), group, target)}[审核] 写入禁言历史失败: {e}")
        return out

async def _mod_variant_strike(host, event, group, rep, settings, variant, result, rec):
        """变体累计升级（strike）：窗口内同一人的疑似变体次数 → 提醒 / 自动禁言。

        设计边界（延续「变体只是疑似」的定位）：
        - L1 提醒默认开：零风险，只发一条**被动消息** @ 已登记管理员 + 待办高亮；
        - L2 禁言默认关：须同时满足「阈值 + 人工确认过 ≥1 次 + 冷却期外」，缺一不可；
        - 本群无人登记管理员 → 不发提醒（不刷群、也不消耗主动消息配额）。
        """
        info = {"count": 0, "confirmed": 0, "level": moderation.STRIKE_NONE,
                "notified": False, "muted": False, "note": ""}
        if not settings.get("variant_strike_enabled", True):
            return info
        target = str(rep.get("sender") or "")
        if not target:
            return info
        try:
            win = max(1, int(settings.get("variant_strike_window_days") or 7))
        except (TypeError, ValueError):
            win = 7
        summ = host.mod.variant_strike_summary(group, target, window_days=win)
        count = int(summ["count"])
        info["count"] = count
        info["confirmed"] = int(summ["confirmed"])
        try:
            thr = max(1, int(settings.get("variant_strike_threshold") or 3))
        except (TypeError, ValueError):
            thr = 3
        # 只在「首次达阈值」和「阈值整数倍」时提醒（3、6、9…）：既不漏，也不每来一条刷一次群
        crossed = count >= thr and (count == thr or count % thr == 0)
        if crossed:
            info["level"] = moderation.STRIKE_TODO

        # ---------------- L2：自动禁言（默认关） ----------------
        if bool(settings.get("variant_strike_mute", False)) and not settings.get("dry_run", True):
            try:
                mute_thr = max(1, int(settings.get("variant_strike_mute_threshold") or 5))
            except (TypeError, ValueError):
                mute_thr = 5
            try:
                cool = max(0, int(settings.get("variant_strike_cooldown_hours") or 6)) * 3600
            except (TypeError, ValueError):
                cool = 6 * 3600
            exempt = (target in [str(x) for x in (settings.get("whitelist") or [])]
                      or host._is_qq_admin_openid(group, target))
            if count >= mute_thr and not exempt:
                if bool(settings.get("variant_strike_require_confirm", True)) and not summ["confirmed"]:
                    info["note"] = "未自动禁言：需先有 1 次人工确认过的变体记录"
                elif cool and (time.time() - int(summ["last_muted"] or 0)) < cool:
                    info["note"] = f"未自动禁言：冷却中（{int(cool / 3600)} 小时内已处理过）"
                else:
                    try:
                        cap = int(settings.get("max_mute_seconds") or 86400)
                    except (TypeError, ValueError):
                        cap = 86400
                    try:
                        secs = int(settings.get("variant_strike_mute_seconds") or 600)
                    except (TypeError, ValueError):
                        secs = 600
                    secs = max(60, min(secs, cap))
                    api = host._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
                    if api is None:
                        info["note"] = "未自动禁言：无可用 API 客户端"
                    else:
                        expire_ts = time.time() + secs
                        try:
                            await api.set_mute(group, target,
                                               mute_expire_at=QQApi.format_rfc3339(expire_ts), op="add")
                            info["muted"] = True
                            info["level"] = moderation.STRIKE_MUTE
                            info["mute_seconds"] = secs
                            host._add_mute_record(
                                group_openid=group,
                                platform_id=event.get_platform_id(),
                                member_openid=target,
                                username=str(rep.get("sender_name") or ""),
                                reason=f"连续疑似变体 ×{count}（近 {win} 天）",
                                seconds=secs, expire_ts=expire_ts,
                                operator="", operator_name="变体累计升级",
                            )
                            host.logger.info(f"[审核][变体] 累计升级禁言：target={target} count={count} "
                                             f"{secs}s group={group}")
                        except Exception as e:
                            info["note"] = f"未自动禁言：{e}"
                            host.logger.warning(f"[审核][变体] 累计升级禁言失败：{e}")

        # ---------------- L1：被动消息 @ 已登记管理员 ----------------
        mentions = ""
        if info["muted"] or (crossed and bool(settings.get("variant_strike_notify", True))):
            admins = host.mod.get_admins(group)
            if admins:
                # 只 @ 前 10 个，避免消息过长被截断
                mentions = " ".join(f"<@{a['openid']}>" for a in admins[:10])
                info["notified"] = True
            else:
                info["note"] = info["note"] or "本群无人登记管理员，未发送 @ 提醒"
        method = host._variant_method_name(str(variant.get("method") or ""))
        if info["notified"]:
            if info["muted"]:
                text = host._mod_tpl(group, "mod_variant_strike_muted", mentions=mentions,
                                     user=f"<@{target}>", at=target, count=count, days=win,
                                     duration=fmt_duration(int(info.get("mute_seconds") or 0)))
            else:
                text = host._mod_tpl(group, "mod_variant_strike_notify", mentions=mentions,
                                     user=f"<@{target}>", at=target, count=count, days=win, method=method)
            await host._reply(event, text)
        if info["note"]:
            host.logger.info(f"[审核][变体] {info['note']}（group={group} target={target} count={count}）")

        # 回填到本条判定记录：让待办能显示「累计 ×N」并按档位排序
        if rec and rec.get("id"):
            host.mod.update_verdict(int(rec["id"]), variant_strike={
                "count": count,
                "level": info["level"],
                "window_days": win,
                "notified": bool(info["notified"]),
                "muted": bool(info["muted"]),
                "confirmed": int(summ["confirmed"]),
            })
        result["variant_strike_notified"] = bool(info["notified"])
        return info
