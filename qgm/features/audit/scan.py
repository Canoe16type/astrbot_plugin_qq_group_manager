"""巡检自动抽查（机械搬迁）。"""

from __future__ import annotations

from . import basis as audit_basis

import time


async def _maybe_auto_scan(host, event):
        """隐藏开关：本群自动巡检（每群独立，互不影响，默认关）。

        成本控制：默认只跑零成本的「关键词维度」；需要 AI 巡检时，
        在插件页面把该群的 auto_scan_llm 打开（会按 daily_budget 熔断）。
        变体检测（variant_check，默认关）在这里与判定并行、互不影响：
        命中只是「疑似」，只撤回 + 记录 + 进待办，绝不禁言。
        整条链路完全由本群的 auto_scan 开关控制，关了就连变体层一起不跑。
        """
        try:
            g = event.get_group_id()
            if not g:
                return
            st = host._mod_settings(g)
            if not st.get("auto_scan"):
                return
            text = host._extract_text(event)
            images = host._extract_images(event)
            # 纯图片消息也要能进巡检：官方群消息常把「文字 + 图片」拆成两条，
            # 只按 text 判会把图片那条整条丢掉（图片维度等于没开）。
            if (not text and not images) or (not text and not st.get("enable_vision", True)):
                return
            rep = {
                "id": str(getattr(event.message_obj, "message_id", "") or ""),
                "text": text,
                "sender": event.get_sender_id() or "",
                "sender_name": event.get_sender_name() or "",
                "images": images,
                "ts": time.time(),
            }
            # 变体疑似信号：与「是否违规」的判定并行、互不影响（variant_check 默认关，关着就不扫）
            variant = host._mod_variant_scan(g, rep, st)
            if variant:
                host.logger.info(
                    f"[审核][变体] 巡检疑似命中：method={variant['method']} "
                    f"strength={variant['strength']} hits={variant['count']} group={g}"
                )
            verdict = host._mod_review_fast(g, rep, scene="巡检")
            if verdict is None and st.get("auto_scan_llm"):
                verdict = await host._mod_review_llm(
                    event, g, rep, host._mod_context_lines(g, rep, st), "（自动巡检）", scene="巡检"
                )
            if not verdict or not verdict.get("violation"):
                # 没判定违规但疑似变体：单独走「只撤回+记录」分支（不发言、不禁言）
                if variant:
                    await host._mod_auto_variant_only(event, g, rep, st, variant)
                return
            result = await host._mod_execute(event, g, rep, verdict, st)
            # 变体疑似：没被判定路径处理掉的话，按群配置决定是否撤回（只撤回，不禁言）
            if variant and not result["executed"]:
                await host._mod_variant_action(event, g, rep, st, variant, result)
            rec = host.mod.add_verdict({
                "group": g, "msg_id": rep["id"], "reporter": "auto_scan",
                "target": rep["sender"], "target_name": rep["sender_name"],
                **host._verdict_text(rep), "violation": result["violation"],
                "basis": result["basis"], "category": result["category"],
                "confidence": result["confidence"], "reason": result["reason"],
                "rule_name": result["rule_name"],
                "action": str(result.get("action") or ("mute" if result["executed"] else "none")),
                "pending_review": bool(result.get("pending_review")),
                "rule_id": result.get("rule_id") or "",
                "match_by": result.get("match_by") or "none",
                "policy_trace": result.get("policy_trace") or {},
                "ban_seconds": result["seconds"], "skipped_reason": result["skipped_reason"],
                "executed": result["executed"], "dry_run": result["dry_run"],
                "recall_note": result.get("recall_note", ""),
                "from_cache": result["cached"],
                "severity": result.get("severity") or verdict.get("severity") or "",
                "prior_offenses": result.get("prior_offenses") or 0,
                "escalation": result.get("escalation") or 1.0,
                "model": verdict.get("model") or "keyword",
                "variant_hit": bool(variant),
                "variant": ({"method": variant["method"], "strength": variant["strength"],
                             "count": variant["count"], "source": variant["source"]} if variant else None),
                "variant_recalled": bool(result.get("variant_recalled")),
                "variant_note": result.get("variant_note", ""),
                "todo_state": "" if st.get("todo_enabled", True) else "disabled",
            })
            if variant:
                await host._mod_variant_strike(event, g, rep, st, variant, result, rec)
            if result["executed"]:
                await host._reply(event, host._mod_tpl(
                    g, "mod_hit",
                    basis=host.mod.get_dim_names(g).get(str(result["basis"]), "多重维度"),
                    basis_detail=audit_basis.basis_detail(host, g, result),
                    rule_name=result.get("rule_name") or "",
                        category=host.mod.get_cat_names(g).get(str(result["category"]), "违规"),
                    duration=result["duration"], user=rep["sender_name"], reason=result["reason"],
                ))
        except Exception as e:
            host.logger.debug(f"[审核] 自动巡检异常: {e}")
