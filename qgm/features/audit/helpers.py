"""审核辅助：设置读取、模型信息、变体与召回、群规匹配。"""

from __future__ import annotations

import time

from ...kernel.text import _recall_hint

try:
    from .... import lexicon as lexicon_lib, moderation, variant as variant_lib
except ImportError:
    import lexicon as lexicon_lib  # type: ignore
    import moderation  # type: ignore
    import variant as variant_lib  # type: ignore


def _mod_settings(host, group):
        return host.mod.get_settings(group)

def _mod_context_lines(host, group, rep, settings):
        """取被举报消息前后的上下文（keep_context 关闭时返回空）。"""
        if not settings.get("keep_context", True):
            return []
        try:
            n = max(0, int(settings.get("context_size") or 0))
            if n <= 0:
                return []
            lst = host._recent_group_msgs.get(group) or []
            anchor = float(rep.get("ts") or 0)
            idx = len(lst) - 1
            for i, r in enumerate(lst):
                if abs(float(r.get("ts") or 0) - anchor) < 0.5:
                    idx = i
                    break
            start = max(0, idx - n)
            out = []
            for r in lst[start:idx]:
                who = (r.get("sender_name") or r.get("sender") or "群成员")
                txt = (r.get("text") or "").strip()
                if not txt and r.get("images"):
                    txt = "[图片]"
                if txt:
                    out.append(f"{who}：{txt[:120]}")
            return out
        except Exception:
            return []

async def _mod_provider_info(host, event=None, vision=False, group=''):
        """解析本次判定用哪个模型：配置指定 > 群/会话当前对话模型 > 全局默认 > 首个可用对话模型。

        返回 {"id", "display", "source"}；source ∈ config/session/default/fallback/none。
        说明：面板试跑没有 event，以前只能随便挑一个 provider（可能挑到 embedding），
        现在用「群 umo」反查该群正在用的对话模型，做到和群里实际审核同款模型。
        """
        key = "vision_provider_id" if vision else "moderation_provider_id"
        pid = str(host._read_config_value(("moderation", key), default="",
                                          legacy_keys=(key,)) or "").strip()
        if pid:
            return {"id": pid, "source": "config", "display": host._provider_display(pid)}
        umo = event.unified_msg_origin if event is not None else host._group_umo(group)
        if umo:
            try:
                cur = str(await host.context.get_current_chat_provider_id(umo=umo) or "").strip()
                if cur:
                    return {"id": cur, "source": "session", "display": host._provider_display(cur)}
            except Exception as e:
                host.logger.warning(f"[审核] 取当前会话模型失败（umo={umo}），回退默认模型: {e}")
        try:
            dft = str(await host.context.get_current_chat_provider_id(umo="") or "").strip()
            if dft:
                return {"id": dft, "source": "default", "display": host._provider_display(dft)}
        except Exception as e:
            host.logger.warning(f"[审核] 取默认对话模型失败，回退首个可用模型: {e}")
        # 最后兜底：第一个「对话类」provider（跳过 embedding/rerank/stt/tts，避免拿到非对话模型）
        for opt in host._provider_options():
            t = str(opt.get("type") or "").lower()
            if any(k in t for k in ("embedding", "rerank", "stt", "tts", "speech")):
                continue
            return {"id": str(opt.get("id") or ""), "source": "fallback",
                    "display": str(opt.get("display") or "")}
        return {"id": "", "source": "none", "display": ""}

async def _mod_provider_id(host, event=None, vision=False, group=''):
        return str((await host._mod_provider_info(event=event, vision=vision, group=group)).get("id") or "")

def _mod_extra_variant_words(settings: dict):
        """本群自定义关键词里的字面片段（正则语法会被拆掉，只取字面部分）。"""
        out: list[tuple[str, tuple]] = []
        try:
            for cat, pats in (settings.get("keywords") or {}).items():
                c = lexicon_lib.normalize_category(str(cat))
                for p in (pats or []):
                    for frag in variant_lib.literals_from_pattern(str(p)):
                        out.append((frag, (c, frag, "group", "本群自定义")))
        except Exception:
            return []
        return out

def _mod_variant_scan(host, group, rep, settings):
        """变体检测：返回疑似命中摘要（不含命中的具体词），没命中或未开启则 None。

        定位：这是**疑似**信号，不参与「是否违规」的判定，也不影响置信度，
        只用于「撤回 + 记录 + 进待办」。所以即使误报，代价也限于一条撤回，不会误禁言。
        """
        if not settings.get("variant_check", False):
            return None
        text = str(rep.get("text") or "")
        if not text.strip():
            return None
        try:
            host._variant_sync()
            hits = host.variant.scan(
                text,
                extra_words=host._mod_extra_variant_words(settings),
                allow=host.lexicons.allow_words(),
                limit=3,
                pinyin=True,
                confusable=bool(settings.get("variant_confusable", True)),
                abbr=bool(settings.get("variant_abbr", True)),
                min_tokens=int(settings.get("variant_min_chars") or 3),
                allow_fuzzy=bool(settings.get("variant_fuzzy", False)),
            )
        except Exception as e:
            host.logger.warning(f"[变体] 扫描异常，本条跳过变体层：{e}")
            return None
        if not hits:
            return None
        order = {"high": 3, "medium": 2, "low": 1}
        best = max(hits, key=lambda h: order.get(str(h.get("strength")), 0))
        # 说明：命中词条本身一律不进日志/记录/面板（沿用「不展示词条内容」的既有约定）
        return {
            "count": len(hits),
            "method": str(best.get("method") or ""),
            "strength": str(best.get("strength") or ""),
            "category": str(best.get("category") or ""),
            "source": str(best.get("source") or ""),
            "hint": host._variant_method_name(str(best.get("method") or "")),
        }

async def _mod_variant_action(host, event, group, rep, settings, variant, result):
        """变体疑似命中后的动作：**只撤回 + 记录，绝不自动禁言**（惩罚性动作须人工复核）。"""
        result.setdefault("variant_recalled", False)
        result["variant_note"] = ""
        if not settings.get("variant_recall", False):
            result["variant_note"] = "本群未开启「疑似即撤回」，仅记录（可在面板举报设置里开启）"
            return
        if settings.get("dry_run", True):
            result["variant_note"] = "试运行模式，未实际撤回"
            return
        mid = str(rep.get("id") or "")
        if not mid:
            result["variant_note"] = "无法定位被举报消息，未撤回"
            return
        try:
            max_age = float(settings.get("recall_max_age_seconds", 110) or 0)
        except (TypeError, ValueError):
            max_age = 110.0
        age = time.time() - float(rep.get("ts") or 0)
        if max_age > 0 and age > max_age:
            result["variant_note"] = (f"消息已发出 {int(age)} 秒，超出官方 2 分钟撤回时限，未撤回"
                                      f"（可将本群 recall_max_age_seconds 设为 0 强制尝试）")
            return
        api = host._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
        if api is None:
            result["variant_note"] = "无可用 API 客户端，未撤回"
            return
        try:
            await api.recall_group_message(group, mid)
            result["variant_recalled"] = True
            host._add_recall_record(
                group_openid=group,
                platform_id=event.get_platform_id(),
                message_id=mid,
                content=str(rep.get("text") or ""),
                source="variant_auto",
                operator="",
                operator_name="变体检测",
            )
            host.logger.info(
                f"[审核][变体] 已按疑似变体撤回消息 {mid} (group={group}, method={variant.get('method')})"
            )
        except Exception as e:
            result["variant_note"] = _recall_hint(e)
            host.logger.warning(f"[审核][变体] 撤回失败：{result['variant_note']}")

def _mod_keyword_hit(host, text, settings):
        """关键词维度（零成本）：按「本群自定义关键词 → 词库 → 代码内置正则」取第一条命中。

        词库（公开词库/页面上传）走字面量多模式匹配，几万条也是毫秒级；
        代码内置的正则仍然兜底，覆盖「加我vx」「刷单」这类结构化套路。
        """
        try:
            hit = host.lexicons.hit(text, extra_keywords=settings.get("keywords") or {})
        except Exception as e:                                    # 词库坏了不能拖垮审核
            host.logger.warning(f"[词库] 命中检测异常，已跳过词库层: {e}")
            hit = None
        if hit:
            return hit
        return moderation.keyword_hit(text)

def _mod_predict_action(host, group, verdict, rep, prior_override=None):
        """试跑用：与真实执行共用 _mod_plan，只算不执行。"""
        return host._mod_plan(group, verdict, rep, prior_override=prior_override)

async def _mod_try_recall(host, event, group, rep, settings):
        """尽力撤回被处罚消息，返回一行说明（失败不影响后续动作）。"""
        mid = str(rep.get("id") or "")
        if not mid:
            return ""
        try:
            max_age = float(settings.get("recall_max_age_seconds", 110) or 0)
        except (TypeError, ValueError):
            max_age = 110.0
        age = time.time() - float(rep.get("ts") or 0)
        if max_age > 0 and age > max_age:
            note = (f"消息已发出 {int(age)} 秒，超出官方 2 分钟撤回时限，跳过撤回"
                    f"（可将本群 recall_max_age_seconds 设为 0 强制尝试）")
            host.logger.info(f"[审核] {note}")
            return note
        api = host._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
        if api is None:
            return "无可用 API 客户端，跳过撤回"
        try:
            ok, err = await host._run_action(api.recall_group_message(group, mid))
            if not ok:
                return err
            host.logger.info(f"[审核] 已撤回消息 {mid} (group={group})")
            host._add_recall_record(
                group_openid=group,
                platform_id=event.get_platform_id(),
                message_id=mid,
                content=str(rep.get("text") or ""),
                source="report_auto",
                operator=event.get_sender_id(),
                operator_name=event.get_sender_name() or "",
            )
            return ""
        except Exception as e:
            note = _recall_hint(e)
            host.logger.warning(f"[审核] 撤回失败（不影响后续动作）: {note}")
            return note

def _mod_group_allowed(host, group):
        """审核相关的群指令同样遵守 basic.target_group_openids 管理名单。"""
        return host._target_group_allowed(group)

async def _mod_auto_variant_only(host, event, group, rep, settings, variant):
        """巡检命中变体、但关键词/模型层没判定违规：只撤回 + 记录 + 进待办。

        与 /举报 的区别：巡检没有举报人可以答复，所以这里**不在群里发言**
        （只有「累计升级」真达阈值时，才按既有规则 @ 已登记管理员），避免自动巡检刷屏。
        定位同前：疑似信号，绝不禁言（L2 自动禁言仍受 variant_strike_mute 默认关约束）。
        """
        result = {
            "violation": False, "executed": False,
            "basis": moderation.DIM_KEYWORD,
            "category": str(variant.get("category") or ""),
            "severity": str(variant.get("strength") or ""),
            "confidence": 0, "reason": "", "rule_name": "",
            "seconds": 0, "duration": 0,
            "skipped_reason": "巡检疑似变体（未判定违规）",
            "dry_run": bool(settings.get("dry_run")), "cached": False,
        }
        await host._mod_variant_action(event, group, rep, settings, variant, result)
        rec = host.mod.add_verdict({
            "group": group,
            "msg_id": str(rep.get("id") or ""),
            "reporter": "auto_scan",
            "target": rep.get("sender") or "",
            "target_name": rep.get("sender_name") or "",
            **host._verdict_text(rep),
            "violation": False,
            "basis": result["basis"],
            "category": result["category"],
            "severity": result["severity"],
            "confidence": 0,
            "reason": "",
            "rule_name": "",
            "action": "none",
            "ban_seconds": 0,
            "skipped_reason": result["skipped_reason"],
            "executed": False,
            "dry_run": result["dry_run"],
            "from_cache": False,
            "recall_note": "",
            "model": "variant",
            "variant_hit": True,
            "variant": {"method": variant["method"], "strength": variant["strength"],
                        "count": variant["count"], "source": variant["source"]},
            "variant_recalled": bool(result.get("variant_recalled")),
            "variant_note": result.get("variant_note", ""),
            "todo_state": "" if settings.get("todo_enabled", True) else "disabled",
        })
        await host._mod_variant_strike(event, group, rep, settings, variant, result, rec)

def _mod_group_entry(host, group):
        meta = host.mod.get_group_meta(group)
        name = str(meta.get("name") or "")
        alias = str(meta.get("alias") or "")
        return {
            "group": group,
            "platform_id": host._group_bot(group),
            "name": name,
            "alias": alias,
            "display": alias or name,
            "name_state": str(meta.get("name_state") or ""),   # "" 未拉取 / ok / denied(无权限) / failed
            "name_error": str(meta.get("name_error") or ""),
        }
