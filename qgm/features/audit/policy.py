"""处罚计划构建（纯逻辑，无 IO、无全局状态）。

把「判定结果」映射为「处罚计划」的唯一来源；试跑与真实执行共用，
避免两套参数拼装漂移。外部依赖（群规、前科、默认时长）由调用方注入。
"""

from __future__ import annotations


def plan_trace(plan: dict, settings: dict, conf: float) -> dict:
    """写进判定记录的决策依据，便于事后回答「为什么罚成这样」。"""
    return {
        "source": plan.get("source") or "none",
        "rule_id": plan.get("rule_id") or "",
        "rule_name": plan.get("rule_name") or "",
        "match_by": plan.get("match_by") or "none",
        "severity": plan.get("severity") or "",
        "confidence": round(float(conf or 0), 3),
        "threshold": float(settings.get("confidence_threshold") or 0.75),
        "prior": plan.get("prior_offenses") or 0,
        "bump_at": int(settings.get("smart_tier_bump_at") or 2),
        "force_at": int(settings.get("smart_force_mute_priors") or 0),
        "escalation": plan.get("escalation") or 1.0,
        "capped": bool(plan.get("escalation_capped")),
        "smart": bool(settings.get("smart_penalty", False)),
        "action": plan.get("action") or "none",
    }


def build_plan(*, verdict: dict, settings: dict, prior: float, rules, default_seconds: int,
               decide_action, escalate_seconds, fmt_duration, warn=None) -> dict:
    """构建处罚计划。

    参数全部显式传入，便于单测；`decide_action` / `escalate_seconds` / `fmt_duration`
    由调用方注入（目前来自 moderation 模块），避免本模块反向依赖插件根目录。
    """
    st = settings
    smart = bool(st.get("smart_penalty", False))
    sev0 = str(verdict.get("severity") or "")
    plan = {
        "action": "none", "recall": False, "seconds": 0, "duration": "",
        "rule_name": "", "rule_id": "", "match_by": "none", "source": "none",
        "severity": sev0, "confidence": verdict.get("confidence", 0),
        "prior_offenses": prior, "escalation": 1.0, "escalation_capped": False,
        "smart": smart, "reason": "",
    }
    try:
        conf = float(verdict.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0

    if not verdict or not verdict.get("violation"):
        plan["reason"] = "未违规"
        plan["trace"] = plan_trace(plan, st, conf)
        return plan

    thr = float(st.get("confidence_threshold") or 0.75)
    warn_thr = float(st.get("warn_confidence_threshold") or 0.6)
    if conf < thr and not verdict.get("from_cache"):
        if smart and sev0.lower() == "high":
            # 高危但拿不准：先把内容撤下来（可逆、无成员影响），处罚交人工决定
            plan.update(action="todo", recall=True,
                        reason=f"高危但置信度 {conf:.2f} < {thr:.2f}，已撤回并转人工复核")
        elif smart and warn_thr <= conf < thr:
            # 中度以上：撤回 + 警告（内容仍有风险）；轻微：只警告，不动消息
            plan.update(action="warn", recall=sev0.lower() != "low", source="smart",
                        severity=sev0 or "medium",
                        reason=f"置信度 {conf:.2f} 低于阈值 {thr:.2f}，仅警告不处罚")
        else:
            plan.update(action="none", reason=f"置信度 {conf:.2f} < 阈值 {thr:.2f}")
        plan["trace"] = plan_trace(plan, st, conf)
        return plan

    dec = decide_action(
        verdict,
        rules=rules,
        max_seconds=int(st.get("max_mute_seconds") or 604800),
        default_seconds=default_seconds,
        smart=smart,
        high_conf=thr,
        prior_offenses=prior,
        prior_bump_at=int(st.get("smart_tier_bump_at") or 2),
        force_mute_priors=int(st.get("smart_force_mute_priors") or 0),
        rule_floor_high=bool(st.get("rule_floor_high", True)),
    )
    kind = str(dec.get("action") or "mute")
    plan.update({
        "action": kind,
        "recall": bool(dec.get("recall")),
        "seconds": int(dec.get("seconds") or 0) if kind == "mute" else 0,
        "duration": fmt_duration(int(dec.get("seconds") or 0)) if kind == "mute" else "",
        "rule_name": dec.get("rule_name") or "",
        "rule_id": dec.get("rule_id") or "",
        "match_by": dec.get("match_by") or "none",
        "source": dec.get("source") or "legacy",
        "severity": dec.get("severity") or sev0,
        "reason": dec.get("skipped_reason") or "",
    })

    if kind == "mute" and prior:
        before = int(plan["seconds"] or 0)
        try:
            if smart and not st.get("escalate_enabled", False):
                max_mult = float(st.get("smart_max_multiplier") or 3.0)
                secs, mult = escalate_seconds(
                    before, prior, step=0.5, max_multiplier=max_mult,
                    cap=int(st.get("max_mute_seconds") or 604800),
                )
                intended = min(1.0 + float(prior) * 0.5, max_mult)
            else:
                secs, mult = escalate_seconds(
                    before, prior,
                    step=float(st.get("escalate_step") or 1.0),
                    max_multiplier=float(st.get("escalate_max_multiplier") or 4.0),
                    cap=int(st.get("max_mute_seconds") or 604800),
                )
                intended = mult
            if secs != before:
                plan["seconds"] = secs
                plan["duration"] = fmt_duration(secs)
                plan["escalation"] = mult
            elif intended > 1.0:
                plan["escalation"] = intended
                plan["escalation_capped"] = True
        except Exception as e:  # noqa: BLE001 - 加刑失败不应阻断处罚
            if warn:
                warn(f"[审核] 累犯加刑计算失败，按原时长执行：{e}")

    plan["trace"] = plan_trace(plan, st, conf)
    return plan
