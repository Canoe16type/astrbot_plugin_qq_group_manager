"""审核/群规指令实现。"""

from __future__ import annotations

import re
import time

from ...kernel.timefmt import fmt_duration
from .rules import RULE_USAGE_ADD, RULE_USAGE_SET, parse_rule_spec


async def _cmd_mod(host, event, parts=None):
        g = event.get_group_id()
        if not g:
            return
        sub = parts[1] if len(parts) > 1 else ""
        st = host._mod_settings(g)
        if sub == "试跑":
            val = (parts[2] if len(parts) > 2 else "").strip()
            if val in ("开", "on", "true", "1"):
                st = host.mod.set_settings(g, {"dry_run": True})
                await host._reply(event, "已开启试运行：只判定不执行。")
            elif val in ("关", "off", "false", "0"):
                host.mod.set_settings(g, {"dry_run": False})
                await host._reply(event, "已关闭试运行：判定后将真实撤回/禁言，请确认群规已调好。")
            else:
                await host._reply(event, "用法：/群管 审核 试跑 开|关")
            return
        if sub == "巡检":
            val = (parts[2] if len(parts) > 2 else "").strip()
            if val in ("开", "on", "true", "1"):
                host.mod.set_settings(g, {"auto_scan": True})
                await host._reply(event, "已开启本群关键词巡检（仅本群生效，零成本；AI 巡检请在插件页面开启）。")
            elif val in ("关", "off", "false", "0"):
                host.mod.set_settings(g, {"auto_scan": False})
                await host._reply(event, "已关闭本群巡检，恢复为「仅举报才审核」。")
            else:
                await host._reply(event, "用法：/群管 审核 巡检 开|关")
            return
        if sub == "自我介绍":
            await host._reply(event, "正在发送本群群管功能介绍…")
            if await host._send_group_intro(g, event.get_platform_id()):
                host._mark_intro_done(g, "manual")
                await host._reply(event, "已发送 ✅ 此后本群不再触发「首条消息欢迎」。")
            else:
                await host._reply(
                    event,
                    "发送失败。主动消息要求群内 24 小时内有人 @ 过机器人（11251），"
                    "先在本群 @ 我一下再试；入群事件那条路不受此限制。",
                )
            return
        if sub == "测试":
            text = " ".join(parts[2:]).strip()
            # 可选：在末尾加「前科=3」模拟该成员已有 3 次前科（不写任何数据）
            prior_override = None
            m_prior = re.search(r"(?:前科|prior)=(\d{1,3})", text)
            if m_prior:
                prior_override = int(m_prior.group(1))
                text = (text[:m_prior.start()] + text[m_prior.end():]).strip()
            if not text:
                await host._reply(event, "用法：/群管 审核 测试 <消息内容> [前科=3]")
                return
            rep = {"text": text, "sender": "test", "sender_name": "测试用户", "images": [], "ts": time.time(), "id": ""}
            v = host._mod_review_fast(g, rep, respect_switches=False, scene="审核测试") or await host._mod_review_llm(
                event, g, rep, [], "（测试）", respect_switches=False, scene="审核测试")
            if not v:
                await host._reply(event, "测试失败：模型未返回结果。")
                return
            d = host._mod_predict_action(g, v, rep, prior_override=prior_override)
            try:
                vh = host._mod_variant_scan(g, rep, {**st, "variant_check": True})
            except Exception:
                vh = None
            vh_text = (f"变体层：疑似命中（{vh['hint']}｜强度 {vh['strength']}｜{vh['count']} 处）"
                       if vh else "变体层：未命中")
            await host._reply(event, (
                f"【测试判定】违规={v.get('violation')} 依据={host.mod.get_dim_names(g).get(str(v.get('basis')),'')} "
                f"类型={v.get('category')} 严重度={v.get('severity')} 置信度={v.get('confidence')}" + chr(10) + ""
                f"理由：{v.get('reason')}" + chr(10) +
                f"拟执行：{d['action']} {d['duration']}"
                + (f"（前科 {d['prior_offenses']} 次，倍率 ×{d['escalation']}）" if d["escalation"] and d["escalation"] != 1.0 else "")
                + ("（智能分级已开启）" if d["smart"] else "（智能分级未开启）")
                + "（试跑不执行任何动作）" + chr(10) +
                vh_text
            ))
            return
        # 默认：状态
        s = host.mod.stats(g)
        await host._reply(event, (
            "【本群审核状态】" + chr(10) + ""
            f"举报审核：{'开启' if st.get('enabled') else '关闭'}｜试运行：{'开' if st.get('dry_run') else '关'}"
            f"｜巡检：{'开' if st.get('auto_scan') else '关'}" + chr(10) + ""
            f"群规：{len(host.mod.get_rules(g))} 条｜阈值：{st.get('confidence_threshold')}｜上限：{fmt_duration(int(st.get('max_mute_seconds') or 0))}" + chr(10) + ""
            f"累计：{s['total']} 次（违规 {s['hit']}）｜今日：{s['today_total']} 次（违规 {s['today_hit']}）" + chr(10) + ""
            f"变体检测：{'开' if st.get('variant_check') else '关'}"
            f"（命中即撤回：{'开' if st.get('variant_recall') else '关'}）"
            f"｜待办：{s.get('todo_open', 0)} 条｜复犯累进：{'开' if st.get('escalate_enabled') else '关'}" + chr(10) + ""
            f"用法：/群管 审核 试跑 开|关 ｜ /群管 审核 巡检 开|关 ｜ /群管 审核 测试 <内容> ｜ /群管 审核 自我介绍"
        ))

async def _cmd_rules(host, event, parts=None):
        event.call_llm = True
        g = event.get_group_id()
        if not g:
            return
        if not host._mod_group_allowed(g):
            return
        sub = parts[1] if len(parts) > 1 else ""
        rules = host.mod.get_rules(g)

        if not sub or sub in ("查看", "列表"):
            if not rules:
                await host._reply(event, host._mod_tpl(g, "mod_rules_empty"))
                return
            lines = [f"【本群群规】共 {len(rules)} 条"]
            for i, r in enumerate(rules, 1):
                flag = "" if r.get("enabled", True) else "（已停用）"
                lines.append(
                    f"{i}. {r.get('name')}{flag}｜{r.get('desc','')}"
                    f"｜{host.mod.get_cat_names(g).get(str(r.get('category')), r.get('category'))}"
                    f"｜{fmt_duration(int(r.get('seconds') or 0))}"
                )
            await host._reply(event, chr(10).join(lines))
            return

        # 以下为写操作，需要管理员
        if not host._is_admin(event):
            await host._reply(event, host._mod_tpl(g, "mod_need_admin"))
            return

        if sub in ("设置", "修改"):
            text = " ".join(parts[2:]).strip()
            if not text:
                await host._reply(event, RULE_USAGE_SET)
                return
            items = [x.strip() for x in re.split("[" + chr(10) + ";]", text) if x.strip()]
            new_rules, bad = [], []
            for it in items:
                name, desc, secs, err = parse_rule_spec(it)
                if err:
                    bad.append(it)
                    continue
                new_rules.append({
                    "name": name, "desc": desc or name,
                    "category": "other", "severity": "medium",
                    "seconds": int(secs or 3600), "enabled": True,
                })
            for r in new_rules:
                host.mod.add_rule(g, r)
            if not new_rules:
                await host._reply(event, RULE_USAGE_SET)
                return
            msg = f"已保存 {len(new_rules)} 条群规，当前共 {len(host.mod.get_rules(g))} 条。"
            if bad:
                msg += chr(10) + f"有 {len(bad)} 行没看懂，已跳过：" + " / ".join(bad[:3])
            await host._reply(event, msg)
            return

        if sub in ("添加", "新增", "add"):
            text = " ".join(parts[2:]).strip()
            name, desc, secs, err = parse_rule_spec(text)
            if err:
                tip = RULE_USAGE_ADD
                if err == "name_too_long":
                    tip = ("规则名太长了（超过 24 字），中间八成漏了竖线分隔。" + chr(10) + RULE_USAGE_ADD)
                await host._reply(event, tip)
                return
            rule = host.mod.add_rule(g, {
                "name": name, "desc": desc,
                "category": "other", "severity": "medium",
                "seconds": int(secs or 3600), "enabled": True,
            })
            # 回显完整内容：让用户当场看出说明和时长有没有被吃掉
            await host._reply(event, (
                f"已添加群规 {rule['id']}" + chr(10) +
                f"名称：{rule['name']}" + chr(10) +
                f"说明：{rule['desc'] or '（未填，按名称判定）'}" + chr(10) +
                f"判定命中后禁言：{fmt_duration(int(rule['seconds']))}"
            ))
            return

        if sub in ("删除", "移除"):
            key = parts[2] if len(parts) > 2 else ""
            removed = host.mod.delete_rule(g, key) if key else None
            await host._reply(event, f"已删除群规：{removed.get('name')}" if removed else "没找到这条群规（可用序号或 id）")
            return

        if sub == "清空":
            host.mod.set_rules(g, [])
            await host._reply(event, "已清空本群群规。")
            return

        await host._reply(event, (
            "群规指令怎么用：" + chr(10) +
            "· /群管 群规            看现有群规" + chr(10) +
            "· /群管 群规 添加 刷屏 | 连续刷无意义内容 | 10分钟" + chr(10) +
            "· /群管 群规 删除 2     删第 2 条" + chr(10) +
            "· /群管 群规 清空       全删"
        ))
