"""举报审核编排：resolve → judge → plan → execute → record → reply。

从 main.py 机械搬迁，行为与 1.6.0 完全一致；后续再按阶段拆成 collect/judge/apply。
"""

from __future__ import annotations

from . import actions as audit_actions
from . import basis as audit_basis
from . import detectors as audit_detectors

try:
    from .... import moderation
except ImportError:  # 测试把插件目录直接加入 sys.path 时
    import moderation  # type: ignore


async def report_violation(host, event, parts):
    """/举报：引用违规消息 → 群规审核（原实现曾误命名为 _cmd_report，
    与「/群管 汇报」的日报方法重名，被后者覆盖导致 /汇报 直接 TypeError）。"""
    event.call_llm = True
    g = event.get_group_id()
    if not g:
        return
    if not host._mod_group_allowed(g):
        return
    settings = host._mod_settings(g)
    if not settings.get("enabled", True):
        await host._reply(event, host._mod_tpl(g, "mod_disabled"))
        return
    reporter = event.get_sender_id() or ""
    if not host.mod.report_allowed(g, reporter, int(settings.get("report_limit_per_hour") or 0)):
        await host._reply(event, host._mod_tpl(g, "mod_report_limit") or "举报太频繁啦，请稍后再试。")
        return
    rep = host._mod_resolve_reported(event)
    if not rep.get("quoted"):
        await host._reply(event, host._mod_tpl(g, "mod_no_quote"))
        return
    has_content = bool((rep.get("text") or "").strip() or rep.get("images"))
    if not has_content or (not rep.get("resolved") and not rep.get("unattributed")):
        # 引用了但既没内容、也无法定位目标：绝不猜人，直接如实提示重新引用
        await host._reply(event, host._mod_tpl(g, "mod_quote_lost"))
        host.mod.add_verdict({
            "group": g, "reporter": reporter, "target": "", "text": "",
            "violation": False, "error": "unresolved_quote",
            "dry_run": bool(settings.get("dry_run")),
            "skipped_reason": "引用消息过旧或无法定位",
        })
        return
    if rep.get("unattributed"):
        # 引用的是 10 分钟以前的消息：正文/图片拿得到，但发送者定位不到。
        # 仍然走一遍判定让人看到结果，但强制不处罚，也不做变体升级。
        host.logger.info(
            "[审核] 未定位到被举报消息的发送者（超出消息缓存窗口），"
            f"本次只判定不处罚: text_len={len(rep.get('text') or '')} images={len(rep.get('images') or [])}"
        )
    mid = str(rep.get("id") or "")
    if mid and mid in host._mod_pending:
        return  # 同一条消息正在审核，忽略重复举报
    if mid:
        host._mod_pending.add(mid)
    note = " ".join(parts[1:])[:200].strip() if len(parts) > 1 else ""
    try:
        host.logger.info(
            f"{host._ctx(event.get_platform_id(), g)}[审核] 收到举报 reporter={host._lb(reporter)} "
            f"text={rep.get('text', '')[:60]!r} images={len(rep.get('images') or [])} "
            f"resolved={bool(rep.get('resolved'))}"
        )
        if settings.get("report_ack_enabled", True):
            # 先回一句受理回执：关键词层可能瞬间出结果，旧逻辑只在走模型时才回，很容易看起来"没反应"
            await host._reply(event, host._mod_tpl(g, "mod_receiving"))
        # 变体检测（疑似信号，与「是否违规」的判定并行、互不影响）：
        # 命中只做 撤回 + 记录 + 进待办，绝不自动禁言。
        variant = None if rep.get("unattributed") else host._mod_variant_scan(g, rep, settings)
        if variant:
            host.logger.info(
                f"[审核][变体] 疑似命中：method={variant['method']} strength={variant['strength']} "
                f"hits={variant['count']} group={g}"
            )
        # 先跑零成本层：命中就一条消息直接出结果，不打扰群
        verdict = host._mod_review_fast(g, rep)
        if verdict is None and not settings.get("enable_model", True):
            # 模型层已关闭：不能只回「受理回执」然后没下文，如实说明只跑了关键词层
            await host._reply(event, host._mod_tpl(g, "mod_layer_off"))
            host.mod.add_verdict({
                "group": g, "msg_id": mid, "reporter": reporter, "target": rep.get("sender"),
                **host._verdict_text(rep), "violation": False,
                "basis": moderation.DIM_KEYWORD, "category": "", "severity": "",
                "confidence": 0, "error": "model_layer_off",
                "skipped_reason": "模型层已关闭，仅跑关键词层（未命中）",
                "dry_run": bool(settings.get("dry_run")),
            })
            return
        if verdict is None:
            verdict = await host._mod_review_llm(event, g, rep, host._mod_context_lines(g, rep, settings), note)
        if verdict is None:
            await host._reply(event, host._mod_tpl(g, "mod_failed", error="模型未返回结果"))
            host.mod.add_verdict({
                "group": g, "msg_id": mid, "reporter": reporter, "target": rep.get("sender"),
                "violation": False, "error": "llm_failed", "dry_run": bool(settings.get("dry_run")),
            })
            return

        # 扩展检测：注册表为空时零开销；只增强、不削弱
        if audit_detectors.count():
            evidences = await audit_detectors.collect(
                host, {"group": g, "rep": rep, "settings": settings, "verdict": verdict})
            verdict = audit_detectors.merge(verdict, evidences)

        result = await host._mod_execute(event, g, rep, verdict, settings)
        # 变体疑似：没被禁言路径处理掉的话，这里按群配置决定是否撤回（只撤回，不禁言）
        if variant and not result["executed"]:
            await host._mod_variant_action(event, g, rep, settings, variant, result)
        # 扩展动作：默认空；失败不影响主处罚
        if audit_actions.count():
            extra = await audit_actions.run(host, {
                "group": g, "rep": rep, "verdict": verdict, "result": result, "settings": settings})
            if extra:
                result["extra_actions"] = extra
        rec = host.mod.add_verdict({
            "group": g,
            "msg_id": mid,
            "reporter": reporter,
            "target": rep.get("sender"),
            "target_name": rep.get("sender_name"),
            **host._verdict_text(rep),
            "violation": result["violation"],
            "basis": result["basis"],
            "category": result["category"],
            "confidence": result["confidence"],
            "reason": result["reason"],
            "rule_name": result["rule_name"],
            "action": str(result.get("action") or ("mute" if result["executed"] else "none")),
            "pending_review": bool(result.get("pending_review")),
            "rule_id": result.get("rule_id") or "",
            "match_by": result.get("match_by") or "none",
            "policy_trace": result.get("policy_trace") or {},
            "ban_seconds": result["seconds"],
            "skipped_reason": result["skipped_reason"],
            "recall_note": result.get("recall_note", ""),
            "executed": result["executed"],
            "dry_run": result["dry_run"],
            "from_cache": result["cached"],
            "severity": result.get("severity") or verdict.get("severity") or "",
            "prior_offenses": result.get("prior_offenses") or 0,
            "escalation": result.get("escalation") or 1.0,
            # 变体检测：只记「方法/强度/条数」，不记命中的具体词（沿用不展示词条的约定）
            "variant_hit": bool(variant),
            "variant": ({"method": variant["method"], "strength": variant["strength"],
                         "count": variant["count"], "source": variant["source"]} if variant else None),
            "variant_recalled": bool(result.get("variant_recalled")),
            "variant_note": result.get("variant_note", ""),
            # 待办状态：本群关掉待办时直接标 disabled，不进队列（记录本身照常留痕）
            "todo_state": "" if settings.get("todo_enabled", True) else "disabled",
            "model": verdict.get("model") or "keyword",
        })
        host.logger.info(
            f"{host._ctx(event.get_platform_id(), g, rep.get('sender'), rec.get('id'))}"
            f"[审核] 已记录 动作={rec.get('action')} 违规={rec.get('violation')} "
            f"档位={rec.get('severity') or '-'} 置信度={float(rec.get('confidence') or 0):.2f} "
            f"执行={rec.get('executed')} 试运行={rec.get('dry_run')}"
        )
        # 变体累计升级：窗口内同一人疑似变体累计到阈值 → 提醒（默认）/ 自动禁言（默认关）
        if variant:
            await host._mod_variant_strike(event, g, rep, settings, variant, result, rec)
        # 只缓存「过了置信度阈值」或试运行下的违规判定：
        # 置信度不足被放过的判定绝不能进缓存，否则它会在 TTL 内绕过阈值直接执行
        if result["violation"] and "置信度" not in str(result["skipped_reason"]):
            host.mod.cache_put(rep.get("text") or "", verdict)
        # 回复话术（被动消息）
        if not result["violation"]:
            if variant:
                if result.get("variant_strike_notified"):
                    return  # 累计提醒消息里已含变体说明，不再重复刷群
                # 疑似变体：优先把这件事说清楚（撤回了还是只记录了，如实告知）
                tpl = "mod_variant_recalled" if result.get("variant_recalled") else "mod_variant_logged"
                await host._reply(event, host._mod_tpl(g, tpl, method=variant["hint"]))
                return
            if settings.get("reply_when_clean", True):
                await host._reply(event, host._mod_tpl(g, "mod_clean"))
            return
        if result["dry_run"]:
            if result.get("ambiguous"):
                await host._reply(
                    event,
                    "⚠️ 这条内容被判为疑似违规，但缓存里有两条以上同文消息，"
                    "无法确定被举报的是哪一条，因此没有执行处罚。\n"
                    "请直接引用目标消息、尽快发一次 `/群管 举报`。",
                )
                return
            if result.get("unattributed"):
                await host._reply(
                    event,
                    "⚠️ 这条内容被判为疑似违规，但引用超出可定位范围（超过 10 分钟），"
                    "无法确定发送者，因此没有执行处罚。\n"
                    "需要处理的话，请当事人重新引用原消息发一次 `/群管 举报`。",
                )
                return
            await host._reply(event, host._mod_tpl(
                g, "mod_dry_run",
                basis=host.mod.get_dim_names(g).get(str(result["basis"]), str(result["basis"] or "")),
                basis_detail=audit_basis.basis_detail(host, g, result),
                rule_name=result.get("rule_name") or "",
                category=host.mod.get_cat_names(g).get(str(result["category"]), str(result["category"] or "")),
                duration=result["duration"],
            ))
            return
        if result.get("pending_review"):
            await host._reply(event, host._mod_tpl(g, "msg_mod_todo"))
            return
        if rep.get("unattributed") and not result["executed"]:
            verdict_txt = (
                host.mod.get_cat_names(g).get(str(result.get("category") or ""), "违规内容")
                if result.get("violation") else "未发现违规"
            )
            await host._reply(event, host._mod_tpl(
                g, "msg_mod_unattributed",
                verdict=verdict_txt,
                reason=str(result.get("skipped_reason") or "被举报消息不在缓存中"),
            ))
            return
        if not result["executed"]:
            # 置信度不足 / 豁免成员 / 执行失败：宁可回复“未处理”也不误报“已禁言”
            rsn = str(result["skipped_reason"] or "")
            if "豁免" in rsn:
                await host._reply(event, host._mod_tpl(g, "mod_exempt"))
            elif "置信度" in rsn:
                if result.get("variant_recalled"):
                    await host._reply(event, host._mod_tpl(g, "mod_variant_recalled",
                                                           method=(variant or {}).get("hint", "变体写法")))
                else:
                    await host._reply(event, host._mod_tpl(g, "mod_uncertain"))
            else:
                await host._reply(event, host._mod_tpl(g, "mod_failed", error=rsn[:40] or "执行未成功"))
            return
        if str(result.get("action") or "") == "warn":
            return          # 警告已经公开发过，别再补一条“已禁言”
        await host._reply(event, host._mod_tpl(
            g, "mod_hit",
            basis=host.mod.get_dim_names(g).get(str(result["basis"]), "多重维度"),
            basis_detail=audit_basis.basis_detail(host, g, result),
            rule_name=result.get("rule_name") or "",
            rule_index=audit_basis.basis_detail(host, g, result),
            category=host.mod.get_cat_names(g).get(str(result["category"]), str(result["category"] or "违规")),
            duration=result["duration"],
            user=rep.get("sender_name") or "",
            reason=result["reason"],
        ))
    except Exception as e:
        host.logger.exception(f"[审核] 处理举报异常: {e}")
        await host._reply(event, host._mod_tpl(g, "mod_failed", error=str(e)[:60]))
    finally:
        if mid:
            host._mod_pending.discard(mid)
