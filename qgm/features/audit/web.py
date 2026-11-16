"""审核域 Web API（群、规则、判定、待办、管理员、话术格式）。"""

from __future__ import annotations

import asyncio
import time

from astrbot.api.web import json_response, request

from ...kernel.timefmt import fmt_duration, fmt_ts
from ...kernel.text import _recall_hint, _refresh_hint
from . import record as audit_record

try:
    from .... import moderation
except ImportError:  # 测试把插件目录直接加入 sys.path 时
    import moderation  # type: ignore
try:
    from ....qq_api import QQApi, QQApiError
except ImportError:
    from qq_api import QQApi, QQApiError  # type: ignore


async def web_mod_groups(host):
        groups = host._all_known_group_list()
        overview = {g["group"]: g for g in host.mod.groups_overview()}
        items = []
        for g in groups:
            ov = overview.get(g)
            item = dict(ov) if ov else {
                "group": g, "enabled": True, "dry_run": True, "auto_scan": False,
                "rule_count": 0, "rules": [], "settings": host.mod.get_settings(g), "stats": host.mod.stats(g),
            }
            item.update(host._mod_group_entry(g))
            items.append(item)
        # 按机器人分桶：不同机器人管不同的群，各自独立、互不干扰
        buckets: dict[str, list] = {}
        for it in items:
            buckets.setdefault(it.get("platform_id") or "", []).append(it)
        bots = []
        for pid in sorted(buckets.keys(), key=lambda x: (x == "", x)):
            b = host._bot_label(pid)
            b["groups"] = buckets[pid]
            b["group_count"] = len(buckets[pid])
            bots.append(b)
        return json_response({"items": items, "bots": bots, "total": len(items)})

async def web_mod_group_rename(host):
        """POST {group, alias} 手动备注群名（仅面板显示，不改变 openid）。"""
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "missing group"})
        host.mod.set_group_alias(g, str(payload.get("alias") or ""))
        return json_response({"ok": True, **host._mod_group_entry(g)})

async def web_mod_group_names_refresh(host):
        """POST {group?|platform_id?|all?, force?} 拉群名 + 机器人昵称。

        默认是「按需补齐」：已经有群名的群直接跳过，不重复打接口；
        需要强制重拉时带 force=true。
        群名接口（GET /v2/groups/{openid}/info）仅白名单机器人可用，
        无权限时返回 11253，这里记为 denied 并如实上报，页面可改用手动备注。
        """
        payload = await request.json(default={})
        one = str(payload.get("group") or "")
        host._bind_log_group(one)
        if one:
            targets = [one]
        else:
            targets = host._all_known_group_list()
            pid_filter = str(payload.get("platform_id") or "")
            if pid_filter:
                targets = [x for x in targets if host._group_bot(x) == pid_filter]
        targets = targets[:60]
        # 已经拿到名字的群不再打扰接口（群名接口 30 QPM，且群名不会频繁变），
        # 需要重拉时显式带 force=true（页面「重新拉取本群群名」或群内 /群名）。
        force = bool(payload.get("force"))
        skipped = 0
        if not force:
            keep = []
            for g in targets:
                m = host.mod.get_group_meta(g)
                if m.get("name") or m.get("name_state") == "denied":
                    skipped += 1
                    continue
                keep.append(g)
            targets = keep
        updated, denied, no_bot, failed, seen_bots = 0, 0, 0, [], set()
        first_error = ""
        for grp in targets:
            pid = host._group_bot(grp)
            if not pid:
                no_bot += 1
                continue
            if pid not in seen_bots:
                seen_bots.add(pid)
                try:
                    await host._refresh_bot_name(pid)
                except Exception as e:      # noqa: BLE001
                    host.logger.warning(f"[审核] 拉机器人昵称失败: {e}")
            try:
                api = host._get_api(pid)
            except QQApiError as e:
                failed.append({"group": grp, "error": str(e)})
                continue
            data, err = await api.fetch_group_info(grp)
            if data and data.get("group_name"):
                host.mod.set_group_meta(grp, name=str(data.get("group_name"))[:60],
                                        platform_id=pid, name_state="ok", name_error="")
                updated += 1
            else:
                code = getattr(err, "code", None)
                is_denied = code == 11253
                msg = str(err) if err else "未知错误"
                host.mod.set_group_meta(grp, platform_id=pid,
                                        name_state=("denied" if is_denied else "failed"),
                                        name_error=msg[:200])
                if is_denied:
                    denied += 1
                else:
                    failed.append({"group": grp, "error": msg})
                first_error = first_error or msg
            await asyncio.sleep(0.08)   # 群信息接口 30 QPM，留点余量
        bots = [host._bot_label(pid) for pid in sorted(seen_bots)]
        return json_response({
            "ok": True, "checked": len(targets), "updated": updated,
            "denied": denied, "no_bot": no_bot, "failed": failed[:10],
            "skipped": skipped, "force": force,
            "message": _refresh_hint(updated, denied, no_bot, failed, first_error, skipped),
            "bots": bots,
        })

async def web_mod_detail(host):
        g = request.query.get("group", "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "missing group"})
        return json_response({
            **host._mod_group_entry(g),
            "bot": host._bot_label(host._group_bot(g)),
            "rules": host.mod.get_rules(g),
            "settings": host.mod.get_settings(g),
            "templates": host.mod.get_templates(g),
            "templates_override": (host.mod.templates.get("groups") or {}).get(g) or {},
            "templates_global": host.mod.templates.get("global") or {},
            "default_templates": moderation.DEFAULT_TEMPLATES,
            "dim_names": host.mod.get_dim_names(g),
            "cat_names": host.mod.get_cat_names(g),
            "default_dim_names": moderation.DEFAULT_DIM_NAMES,
            "default_cat_names": moderation.DEFAULT_CAT_NAMES,
            "admins": host.mod.get_admins(g),
            "stats": host.mod.stats(g),
        })

async def web_mod_rules_save(host):
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        rules = payload.get("rules")
        if not g or not isinstance(rules, list):
            return json_response({"error": "invalid payload"})
        clean = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                continue
            try:
                secs = int(r.get("seconds") or 3600)
            except (TypeError, ValueError):
                secs = 3600
            clean.append({
                "id": str(r.get("id") or f"rule_{i+1:02d}"),
                "name": str(r.get("name") or f"规则{i+1}")[:60],
                "desc": str(r.get("desc") or "")[:600],
                "category": str(r.get("category") or "other")[:30],
                "severity": str(r.get("severity") or "medium")[:10],
                "seconds": max(60, min(secs, 30 * 86400)),
                "enabled": bool(r.get("enabled", True)),
                "keywords": [str(k)[:200] for k in (r.get("keywords") or [])][:20],
            })
        host.mod.set_rules(g, clean)
        return json_response({"saved": True, "count": len(clean)})

async def web_mod_rules_transfer(host):
        """POST /mod/rules/transfer — 把一个群的群规复制到另一个群。

        append 追加（默认，id 自动去重）/ replace 覆盖目标群现有群规。
        源群取的是**已保存**的群规，页面上没保存的改动不算，前端要提示。
        """
        payload = await request.json(default={})
        src = str(payload.get("from") or "").strip()
        dst = str(payload.get("to") or "").strip()
        mode = str(payload.get("mode") or "append").lower()
        ids = payload.get("ids")
        if ids is not None and not isinstance(ids, list):
            ids = None
        if not src or not dst:
            return json_response({"ok": False, "error": "缺少源群或目标群"})
        if src == dst:
            return json_response({"ok": False, "error": "源群和目标群相同"})
        res = host.mod.transfer_rules(src, dst, mode, ids=ids)
        if res.get("ok"):
            scope = f"勾选 {len(ids)} 条" if ids else "全部"
            host.logger.info(
                f"[群规] 转移：{src} → {dst}，{mode} 模式，{scope}，新增 {res['count']} 条，"
                f"目标群现有 {res['total']} 条"
            )
        return json_response(res)

async def web_mod_settings_save(host):
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "missing group"})
        allowed = set(moderation.ModerationStore.DEFAULT_SETTINGS.keys())
        patches = {k: v for k, v in (payload.get("settings") or {}).items() if k in allowed}
        if patches.get("keywords") is not None and not isinstance(patches.get("keywords"), dict):
            patches.pop("keywords", None)
        if "confidence_threshold" in patches:
            try:
                patches["confidence_threshold"] = max(0.0, min(float(patches["confidence_threshold"]), 1.0))
            except (TypeError, ValueError):
                patches.pop("confidence_threshold", None)
        for k in ("context_size", "max_mute_seconds", "report_limit_per_hour", "recall_max_age_seconds",
                  "escalate_window_days", "variant_strike_window_days", "variant_strike_threshold",
                  "variant_strike_mute_threshold", "variant_strike_mute_seconds",
                  "variant_strike_cooldown_hours"):
            if k in patches:
                try:
                    patches[k] = max(0, int(patches[k]))
                except (TypeError, ValueError):
                    patches.pop(k, None)
        # 变体累计升级：把越界值夹到合理区间（阈值 1~50、窗口 1~90 天、冷却 0~168 小时）
        for k, lo, hi in (("variant_strike_window_days", 1, 90),
                          ("variant_strike_threshold", 1, 50),
                          ("variant_strike_mute_threshold", 1, 50),
                          ("variant_strike_mute_seconds", 60, 86400),
                          ("variant_strike_cooldown_hours", 0, 168)):
            if k in patches:
                try:
                    patches[k] = max(lo, min(int(patches[k]), hi))
                except (TypeError, ValueError):
                    patches.pop(k, None)
        if "variant_min_chars" in patches:
            try:
                patches["variant_min_chars"] = max(2, min(int(patches["variant_min_chars"]), 12))
            except (TypeError, ValueError):
                patches.pop("variant_min_chars", None)
        for k in ("escalate_step", "escalate_max_multiplier"):
            if k in patches:
                try:
                    patches[k] = max(0.0, min(float(patches[k]), 20.0))
                except (TypeError, ValueError):
                    patches.pop(k, None)
        if isinstance(patches.get("whitelist"), list):
            patches["whitelist"] = [str(x)[:64] for x in patches["whitelist"]][:200]
        return json_response({"saved": True, "settings": host.mod.set_settings(g, patches)})

async def web_mod_templates_save(host):
        payload = await request.json(default={})
        scope = str(payload.get("scope") or "global")
        values = payload.get("templates") or {}
        if not isinstance(values, dict):
            return json_response({"error": "invalid templates"})
        clean = {}
        for k, v in values.items():
            if k not in moderation.DEFAULT_TEMPLATES and k not in ("dim_names", "cat_names", "mod_report_limit"):
                continue
            if k in ("dim_names", "cat_names"):
                if isinstance(v, dict):
                    clean[k] = {str(a)[:30]: str(b)[:60] for a, b in v.items()}
                continue
            clean[k] = str(v)[:400]
        if scope == "global":
            # 维度/分类显示名是全局的，单独存放
            dims = clean.pop("dim_names", None)
            cats = clean.pop("cat_names", None)
            if dims or cats:
                host.mod.templates["dim_names"] = {**(host.mod.templates.get("dim_names") or {}), **(dims or {})}
                host.mod.templates["cat_names"] = {**(host.mod.templates.get("cat_names") or {}), **(cats or {})}
                host.mod._save(host.mod._tpl_path, host.mod.templates)
            host.mod.set_templates("global", clean)
        else:
            clean.pop("dim_names", None)
            clean.pop("cat_names", None)
            host.mod.set_templates(scope, clean)
        return json_response({"saved": True})

async def web_mod_verdicts_revoke(host):
        """POST /mod/verdicts/revoke {id, reason} — 面板撤销（操作者为当前登录管理员）。"""
        payload = await request.json(default={})
        try:
            vid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return json_response({"ok": False, "error": "invalid id"})
        reason = str(payload.get("reason") or "")[:200]
        rec = host.mod.get_verdict(vid)
        if rec is None:
            return json_response({"ok": False, "error": "not_found"})
        group = str(payload.get("group") or rec.get("group") or "")
        host._bind_log_group(str(group or ""))
        # 面板登录者就是 AstrBot 管理员，按 bot admin 处理（不受「不能撤自己」限制）
        res = await host._revoke_verdict(
            group, vid, operator="panel", operator_name="面板管理员",
            reason=reason, is_bot_admin=True,
        )
        res.pop("record", None)
        return json_response(res)

async def web_mod_verdicts_prune(host):
        """POST /mod/verdicts/prune {days, group} — 删除 N 天以前的记录（仅手动触发）。"""
        payload = await request.json(default={})
        try:
            days = int(payload.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        group = str(payload.get("group") or "")
        host._bind_log_group(str(group or ""))
        if days <= 0:
            return json_response({"ok": False, "error": "days 必须大于 0"})
        removed = host.mod.prune_older_than(days, group)
        host.logger.info(f"[审核] 手动清理判定记录：{days} 天以前，删除 {removed} 条（group={group or '全部'}）")
        return json_response({"ok": True, "removed": removed, "days": days})

async def web_mod_verdicts_export(host):
        """GET /mod/verdicts/export?group=&days=&limit= — 导出判定记录。

        服务端只做筛选与截断，CSV 交给面板前端拼（不依赖 AstrBot 版本的响应体能力）。
        """
        g = str(request.query.get("group", "") or "")
        host._bind_log_group(str(g or ""))
        days = request.query.get("days", 0, type=int) or 0
        limit = min(5000, max(1, request.query.get("limit", 1000, type=int)))
        since = int(time.time()) - days * 86400 if days > 0 else 0
        items = audit_record.filter_records(
            host.mod._iter_verdicts(), group=g, since=since, limit=limit)
        return json_response({"items": items, "total": len(items)})

async def web_mod_verdicts(host):
        g = request.query.get("group", "")
        host._bind_log_group(str(g or ""))
        limit = request.query.get("limit", 100, type=int)
        return json_response({"items": host.mod.list_verdicts(g, limit)})

async def web_mod_feedback(host):
        payload = await request.json(default={})
        try:
            vid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return json_response({"error": "invalid id"})
        label = str(payload.get("label") or "")[:20]
        note = str(payload.get("note") or "")[:300]
        if not host.mod.update_verdict(vid, feedback=label, feedback_note=note):
            return json_response({"error": "not found"})
        # 人工标注回灌到提示词示例（只做提示，不自动改判定规则）。
        # 以前是往内存里 += 一行，重启就丢；现在从判定记录重建，持久化生效。
        host._rebuild_mod_few_shot()
        return json_response({"saved": True, "label": label,
                              "samples": len(host.mod.feedback_samples(limit=20).get(label) or [])})

async def web_mod_admins(host):
        """GET /mod/admins?group=xxx 变体提醒名单（群内管理员用 /管理登记 自助登记）。"""
        g = request.query.get("group", "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "missing group"})
        items = host.mod.get_admins(g)
        return json_response({"items": items, "count": len(items),
                              "notify": bool(host.mod.get_settings(g).get("variant_strike_notify", True))})

async def web_mod_admins_remove(host):
        """POST /mod/admins/remove 把某人移出提醒名单。

        body: {group, openid}
        成员自己发「/管理登记 取消」也有同样效果，这里是为了不用麻烦他本人。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        openid = str(payload.get("openid") or "")
        if not g or not openid:
            return json_response({"error": "invalid payload"})
        removed = host.mod.unregister_admin(g, openid)
        items = host.mod.get_admins(g)
        return json_response({"removed": removed, "items": items, "count": len(items)})

async def web_mod_todo(host):
        """GET /mod/todo?group=xxx 待办队列（低置信未处理 + 变体疑似，都没人工处理过的）。"""
        g = request.query.get("group", "")
        host._bind_log_group(str(g or ""))
        if not g:
            return json_response({"error": "missing group"})
        limit = request.query.get("limit", 100, type=int)
        items = host.mod.todo_items(g, limit)
        for it in items:
            it["created_at_text"] = fmt_ts(it.get("created_at") or 0, "%m-%d %H:%M")
            it["suggest_text"] = fmt_duration(int(it.get("suggest_seconds") or 0))
        return json_response({"items": items, "count": host.mod.todo_count(g),
                              "window_days": host.mod.TODO_WINDOW_DAYS})

async def web_mod_todo_handle(host):
        """POST /mod/todo/handle 处理待办。

        body: {group, ids: [判定记录 id], action: ignore|recall|mute, seconds?, operator?}
        - ignore：只标记「已看过/不处理」，不动消息、不禁言；
        - recall：按群配置撤回该消息（人工确认版撤回）；
        - mute：人工确认后禁言（秒数取 body.seconds，缺省用严重度建议值）。
        无论哪种都会把对应记录标记为已处理，从待办里移出。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        raw_ids = payload.get("ids") or ([payload.get("id")] if payload.get("id") else [])
        action = str(payload.get("action") or "ignore").lower()
        operator = str(payload.get("operator") or "面板")[:64]
        if not g or not raw_ids:
            return json_response({"error": "missing group or ids"})
        try:
            want = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            return json_response({"error": "invalid ids"})
        if action not in ("ignore", "recall", "mute"):
            return json_response({"error": "invalid action"})

        items = {int(i.get("id") or 0): i for i in host.mod.todo_items(g, limit=1000)}
        picked = [items[i] for i in want if i in items]
        if not picked:
            return json_response({"error": "待办记录不存在或已处理"})

        done: list[dict] = []
        muted = recalled = 0
        if action in ("recall", "mute"):
            api = host._api_for_group_or_default(g)
            if api is None:
                return json_response({"error": "无可用 API 客户端"})

        for it in picked:
            vid = int(it.get("id") or 0)
            target = str(it.get("target") or "")
            note = ""
            if action == "recall":
                # 找到被举报消息 ID（判定记录里存的是 msg_id）
                mid = ""
                rec = host.mod.get_verdict(vid)
                mid = str((rec or {}).get("msg_id") or "")
                if mid:
                    try:
                        await api.recall_group_message(g, mid)
                        host._add_recall_record(group_openid=g, platform_id=host._group_bot(g),
                                                message_id=mid, content=str(it.get("text") or ""),
                                                source="todo_manual", operator=operator,
                                                operator_name="待办处理")
                        recalled += 1
                        note = "已撤回"
                    except Exception as e:                       # noqa: BLE001
                        note = _recall_hint(e)
                else:
                    note = "记录里没有消息 ID（引用定位失败），未撤回"
            elif action == "mute":
                if not target:
                    note = "无法确定被举报消息的发送者，未禁言"
                else:
                    try:
                        secs = int(payload.get("seconds") or it.get("suggest_seconds") or 600)
                    except (TypeError, ValueError):
                        secs = 600
                    secs = max(60, min(secs, int(host._mod_settings(g).get("max_mute_seconds") or 86400)))
                    expire_ts = time.time() + secs
                    try:
                        await api.set_mute(g, target, mute_expire_at=QQApi.format_rfc3339(expire_ts), op="add")
                        host._add_mute_record(
                            group_openid=g, platform_id=host._group_bot(g), member_openid=target,
                            username=str(it.get("target_name") or ""),
                            reason=f"待办人工处理：{str(it.get('reason') or '')[:100]}",
                            seconds=secs, expire_ts=expire_ts,
                            operator=operator, operator_name="待办处理",
                        )
                        muted += 1
                        note = f"已禁言 {fmt_duration(secs)}"
                    except Exception as e:                       # noqa: BLE001
                        note = f"禁言失败：{e}"
            else:
                note = "已标记为不处理"
            done.append({"id": vid, "ok": not note.startswith(("撤回失败", "禁言失败", "无可用")), "note": note})

        host.mod.mark_todo(want, "done" if action != "ignore" else "ignored",
                           operator=operator, note=f"{action}: {','.join(d['note'] for d in done)[:120]}")
        host.logger.info(f"[审核] 待办处理：group={g} action={action} 条数={len(done)} "
                         f"禁言={muted} 撤回={recalled}")
        return json_response({"handled": len(done), "muted": muted, "recalled": recalled,
                              "items": done, "count": host.mod.todo_count(g)})

async def web_mod_stats(host):
        g = request.query.get("group", "")
        host._bind_log_group(str(g or ""))
        return json_response({"stats": host.mod.stats(g), "groups": len(host._all_known_group_list())})

async def web_mod_test(host):
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        host._bind_log_group(str(g or ""))
        text = str(payload.get("text") or "")
        try:
            prior_override = int(payload.get("prior")) if payload.get("prior") not in (None, "") else None
        except (TypeError, ValueError):
            prior_override = None
        if not g or not text:
            return json_response({"error": "missing group or text"})
        rep = {"text": text, "sender": "test", "sender_name": "测试用户", "images": [], "ts": time.time(), "id": ""}
        v = host._mod_review_fast(g, rep, respect_switches=False, scene="页面试跑")
        used_llm = False
        pinfo: dict = {"id": "", "display": "", "source": "none"}
        if not v:
            # 模型解析：与群内实际审核同一条链路（配置 > 该群当前对话模型 > 全局默认），
            # 不再像以前那样在面板里随便挑第一个 provider。
            pinfo = await host._mod_provider_info(group=g)
            if not pinfo.get("id"):
                return json_response({"error": "没有可用的对话模型：请先在 AstrBot 里配置 Provider，或在「审核模型」里手动指定"})
            v = await host._mod_review_llm(None, g, rep, [], "（页面试跑）",
                                           pid_override=str(pinfo.get("id") or ""),
                                           respect_switches=False, scene="页面试跑")
            used_llm = True
        if not v:
            return json_response({"error": "判定失败：模型未返回结果"})
        d = host._mod_predict_action(g, v, rep, prior_override=prior_override)
        # 变体层也一起试跑：不看本群开关（页面测试就是要看效果），只借本群的阈值配置
        try:
            vopts = {**host._mod_settings(g), "variant_check": True}
            variant = host._mod_variant_scan(g, rep, vopts)
        except Exception:
            variant = None
        return json_response({
            "verdict": v,
            "action": d,
            "duration": d.get("duration") or "",
            "used_llm": used_llm,
            "provider": pinfo,
            "variant": variant,
        })

async def web_mod_verdicts_delete(host):
        """POST /mod/verdicts/delete body={'id': 12}

        只删本地这条判定留痕（省空间），不影响已经生效的禁言/撤回动作，
        也不会改变模型判定逻辑。
        """
        payload = await request.json(default={})
        try:
            vid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return json_response({"ok": False, "message": "记录 id 无效"})
        if not vid:
            return json_response({"ok": False, "message": "缺少记录 id"})
        ok = host.mod.delete_verdict(vid)
        if ok:
            host.logger.info(f"WebUI 删除判定记录 #{vid}")
        return json_response({"ok": ok, "id": vid,
                              "message": "已删除该条判定记录" if ok else "没找到这条记录（可能已被删除）"})

async def web_mod_verdicts_clear(host):
        """POST /mod/verdicts/clear body={'group': 'xxx'}

        不传 group 时清空全部群的判定记录，传则只清该群。仅删本地留痕。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "").strip()
        host._bind_log_group(str(g or ""))
        removed = host.mod.clear_verdicts(g)
        host.logger.info(f"WebUI 清空判定记录：{removed} 条（{'群 ' + g if g else '全部群'}）")
        return json_response({"ok": True, "cleared": removed, "group": g,
                              "message": f"已清空 {removed} 条判定记录"})

async def web_tpl_formats(host):
        """GET /templates/formats：消息模板清单 + 各自配置的发送格式。"""
        schema = host._conf_schema()
        group = schema.get("templates") if isinstance(schema.get("templates"), dict) else {}
        items_schema = group.get("items") if isinstance(group.get("items"), dict) else {}
        templates = []
        for key, ischema in items_schema.items():
            if not isinstance(ischema, dict):
                continue
            meta = host._tpl_formats.get(key) or {}
            value = host._read_config_value(
                ("templates", key), default=ischema.get("default", ""), legacy_keys=(key,)
            )
            if key in moderation.DEFAULT_TEMPLATES:
                # 审核话术的实际生效值可能是面板设的全局话术，以它为准回显
                override = ((host.mod.templates or {}).get("global") or {}).get(key)
                if override:
                    value = override
            templates.append({
                "key": key,
                "description": str(ischema.get("description") or key),
                "hint": str(ischema.get("hint") or ""),
                "value": "" if value is None else str(value),
                "default": "" if ischema.get("default") is None else str(ischema.get("default")),
                "format": host._tpl_format(key),
                "url": str(meta.get("url") or ""),
                "width": int(meta.get("width") or 0),
                "height": int(meta.get("height") or 0),
            })
        return json_response({"ok": True, "kinds": list(host.TPL_FORMAT_KINDS), "templates": templates})

async def web_tpl_formats_save(host):
        """POST /templates/formats/save：保存模板格式，可同时保存内容。

        body: {"items":[{"key":"msg_help","type":"markdown","url":"","width":0,"height":0,"value":"..."}]}
        兼容单项写法 {"key": ..., "type": ...}
        """
        payload = await request.json(default={}) or {}
        raw_items = payload.get("items")
        if isinstance(raw_items, dict):
            raw_items = [{"key": k, **(v if isinstance(v, dict) else {})} for k, v in raw_items.items()]
        if not isinstance(raw_items, list):
            raw_items = [payload]
        raw_items = [i for i in raw_items if isinstance(i, dict)]

        valid_keys = host._tpl_keys()
        saved: list[str] = []
        errors: list[str] = []
        content_changed = False
        for item in raw_items:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            if key not in valid_keys:
                errors.append(f"未知模板：{key}")
                continue
            meta = dict(host._tpl_formats.get(key) or {})
            if "type" in item:
                kind = str(item.get("type") or "text").strip().lower()
                meta["type"] = kind if kind in host.TPL_FORMAT_KINDS else "text"
            if "url" in item:
                meta["url"] = str(item.get("url") or "").strip()
            for dim in ("width", "height"):
                if dim in item:
                    try:
                        meta[dim] = max(0, int(item.get(dim) or 0))
                    except (TypeError, ValueError):
                        meta[dim] = 0
            host._tpl_formats[key] = meta
            if "value" in item:
                try:
                    value = str(item.get("value") or "")
                    host._write_config_value("templates", key, value)
                    if key in moderation.DEFAULT_TEMPLATES:
                        # 审核话术由 moderation 存储渲染（_mod_tpl），配置只是它的一个入口。
                        # 这里同步写一份到全局话术，否则「面板保存成功但群里没变」。
                        host.mod.set_templates("global", {key: value})
                    content_changed = True
                except Exception as e:
                    errors.append(f"{key} 内容保存失败: {e}")
            saved.append(key)

        if saved:
            try:
                host._save_tpl_formats()
            except Exception as e:
                return json_response({"ok": False, "message": f"格式写入失败: {e}"})
            if content_changed:
                try:
                    host.config.save_config()
                except Exception as e:
                    host.logger.warning(f"保存配置失败: {e}")
                    errors.append(f"内容写入配置文件失败: {e}")
        return json_response({"ok": bool(saved) and not errors, "saved": saved, "errors": errors})
