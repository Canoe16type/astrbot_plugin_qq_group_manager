"""审核判定层：L0 关键词/缓存、L2 语义、L3 多模态。

从 main.py 机械搬迁；模型调用、解析与日志行为保持不变。
"""

from __future__ import annotations

import asyncio
import time

try:
    from .... import moderation
except ImportError:  # 测试把插件目录直接加入 sys.path 时
    import moderation  # type: ignore

from . import categories


def review_fast(host, group: str, rep: dict, respect_switches: bool = True, scene: str = "举报"):
        """L0 关键词 + L1 缓存：零成本，命中直接出结果。

        respect_switches=False 时忽略「关键词层」开关（供 /审核 测试、页面试跑等诊断入口用）。
        命中时会写一条 INFO，说明「这次为什么没调模型」——否则日志里只剩「收到举报」，
        管理员无从判断是走了关键词层还是模型压根没被调用。
        """
        text = rep.get("text") or ""
        settings = host._mod_settings(group)
        ctx = host._ctx("", group)
        hit = None
        if (not respect_switches) or settings.get("enable_keyword", True):
            hit = host._mod_keyword_hit(text, settings)
        if hit:
            cat = hit["category"]
            src = str(hit.get("source") or "")
            host.logger.info(
                f"{ctx}[{scene}][零成本层] 关键词命中（类型={host.mod.get_cat_names(group).get(cat, cat)}"
                f"{'，来源=' + src if src else ''}），未调用模型"
            )
            return {
                "violation": True,
                "basis": moderation.DIM_KEYWORD,
                "category": cat,
                "severity": "high" if cat in ("ad", "illegal", "porn", "politics") else "medium",
                "confidence": 1.0,
                "reason": host._lex_hit_hint(hit),
                "evidence": text[:80],
                "source": src,
                "from_cache": False,
            }
        cached = host.mod.cache_get(text)
        if cached and cached.get("violation"):
            cached["from_cache"] = True
            host.logger.info(f"{ctx}[{scene}][零成本层] 判定缓存命中，未调用模型")
            return cached
        return None


async def review_llm(host, event, group: str, rep: dict, context_lines: list[str],
                     reporter_note: str = "", pid_override: str = "",
                     respect_switches: bool = True, scene: str = "举报"):
        """L2 语义 / L3 多模态：调模型拿 JSON 判定。返回 None 表示失败或该层已关闭。

        日志口径（管理员最常问的「到底调没调模型、调的哪个、模型说了啥」）：
        - INFO：调用前的 provider / 模式 / 提示词规模，调用后的耗时、token、返回长度、
                解析出的判定结论；每一处「没调模型」的提前返回都写明原因。
        - DEBUG：提示词预览与模型原始返回（可能含群聊原文，默认级别下不落盘）。
        """
        settings = host._mod_settings(group)
        ctx = host._ctx("", group)
        if respect_switches and not settings.get("enable_model", True):
            host.logger.debug(f"{ctx}[{scene}][模型] 模型层已关闭，不调用模型")
            return None  # 模型层已关闭：不发任何请求（省钱、降噪）
        # 次数熔断：当天模型判定次数达到上限后不再调模型（自动降级为仅关键词层）
        daily_limit = int(settings.get("daily_budget") or 0)
        if daily_limit > 0:
            used_today = int(host.mod.stats(group).get("today_llm_calls", 0) or 0)
            if used_today >= daily_limit:
                host.logger.warning(
                    f"{ctx}[{scene}][模型] 群 {group} 已达当日模型判定次数上限 "
                    f"（今日 {used_today}/{daily_limit} 次），跳过模型判定"
                )
                return None

        images = list(rep.get("images") or [])
        if respect_switches and not settings.get("enable_vision", True):
            images = []  # 图片层关闭：图片不进提示词，只判文字
        if not images and not (rep.get("text") or "").strip():
            host.logger.debug(f"{ctx}[{scene}][模型] 无可判定内容（文本为空且图片层关闭），跳过模型调用")
            return None  # 没有任何可判定内容（例如关闭图片层后只剩图片）

        # 并发闸门：同群已在排队/执行中的模型调用达到上限时，本条消息不再排队，
        # 直接退回「零成本层」的结果（关键词没命中就按未违规处理），宁可漏判也不拖垮队列。
        queue_limit = int(settings.get("llm_queue_limit") or 0)
        pending_now = host._mod_llm_pending.get(group, 0)
        if queue_limit > 0 and pending_now >= queue_limit:
            host.logger.info(
                f"{ctx}[{scene}][模型] 同群模型调用排队已满（{pending_now}/{queue_limit}），"
                f"本条跳过模型层，仅按零成本层结果处理"
            )
            return None
        sysp = moderation.build_system_prompt(
            dim_names=host.mod.get_dim_names(group),
            cat_names=host.mod.get_cat_names(group),
            group_rules=host.mod.get_rules(group),
            few_shot=host._mod_few_shot,
            common_ban=str(host._read_config_value(("moderation", "common_ban"), default="", legacy_keys=("common_ban",)) or ""),
            category_block=categories.prompt_block(),
        )
        up = moderation.build_user_prompt(
            message_text=rep.get("text") or "",
            sender_name=rep.get("sender_name") or rep.get("sender") or "",
            context_lines=context_lines,
            reporter_note=reporter_note,
            has_image=bool(images),
        )

        def _usage_text(resp_obj) -> str:
            usage = getattr(resp_obj, "usage", None)
            if usage is None:
                return ""
            try:
                return (f" tokens入={usage.input_other}(缓存 {usage.input_cached})"
                        f" tokens出={usage.output}")
            except Exception:
                return ""

        async def _call(pid_used: str, with_images: list[str]):
            mode = f"多模态({len(with_images)} 张图)" if with_images else "纯文本"
            host.logger.info(
                f"{ctx}[{scene}][模型] 调用模型 provider={pid_used} 模式={mode} "
                f"提示词 {len(sysp) + len(up)} 字（system {len(sysp)} / user {len(up)}）"
            )
            host.logger.debug(
                f"{ctx}[{scene}][模型] 提示词预览 system={sysp[:200]!r} … user={up[:400]!r}"
            )
            t0 = time.perf_counter()
            sem = host._mod_llm_sem.get(group)
            if sem is None:
                sem = host._mod_llm_sem[group] = asyncio.Semaphore(1)
            # 先占位再取锁：pending 计的是「排队 + 执行中」，入口处的阈值判定才准
            host._mod_llm_pending[group] = host._mod_llm_pending.get(group, 0) + 1
            timeout_s = float(settings.get("llm_timeout") or 0)
            try:
                async with sem:
                    pending = host.context.llm_generate(
                        chat_provider_id=pid_used,
                        system_prompt=sysp,
                        prompt=up,
                        image_urls=with_images or None,
                        temperature=0.0,
                        max_tokens=320,
                    )
                    # 超时必须自己兜：provider 卡住会一直占着同群的锁，后面的消息全排不上
                    r = await (asyncio.wait_for(pending, timeout_s) if timeout_s > 0 else pending)
            except asyncio.TimeoutError:
                ms = (time.perf_counter() - t0) * 1000
                host.logger.warning(
                    f"{ctx}[{scene}][模型] 调用超时 provider={pid_used} 模式={mode} "
                    f"耗时 {ms:.0f}ms（阈值 {timeout_s:.0f}s），本条按判定失败处理"
                )
                host._mod_last_llm = {"ts": int(time.time()), "ok": False, "ms": round(ms),
                                      "scene": scene, "provider": pid_used, "error": "timeout"}
                raise
            except Exception as e:
                ms = (time.perf_counter() - t0) * 1000
                host.logger.warning(
                    f"{ctx}[{scene}][模型] 调用异常 provider={pid_used} 模式={mode} "
                    f"耗时 {ms:.0f}ms: {e}"
                )
                host._mod_last_llm = {"ts": int(time.time()), "ok": False, "ms": round(ms),
                                      "scene": scene, "provider": pid_used,
                                      "error": str(e)[:120]}
                raise
            finally:
                host._mod_llm_pending[group] = max(0, host._mod_llm_pending.get(group, 1) - 1)
            text = getattr(r, "completion_text", "") or ""
            host.logger.info(
                f"{ctx}[{scene}][模型] 调用完成 provider={pid_used} 耗时 "
                f"{(time.perf_counter() - t0) * 1000:.0f}ms{_usage_text(r)} 返回 {len(text)} 字"
            )
            host.logger.debug(f"{ctx}[{scene}][模型] 原始返回: {text[:800]!r}")
            host._mod_last_llm = {"ts": int(time.time()), "ok": True,
                                  "ms": round((time.perf_counter() - t0) * 1000),
                                  "scene": scene, "provider": pid_used}
            return r

        pid = pid_override or await host._mod_provider_id(event, vision=bool(images), group=group)
        if not pid:
            host.logger.warning(
                f"{ctx}[{scene}][模型] 找不到可用的对话模型"
                f"（未指定「审核模型」且取不到默认 Provider），跳过模型调用"
            )
            return None
        resp = None
        used_images: list[str] = []
        if images:
            vpid = await host._mod_provider_id(event, vision=True, group=group) if event is not None else pid
            # 多模态模型不支持图片时会直接抛错（官方文档明确），
            # 这里兜底降级为纯文本判定，绝不因此中断审核。
            try:
                resp = await _call(vpid or pid, images)
                used_images = images
            except Exception as e:
                host.logger.warning(f"{ctx}[{scene}][模型] 多模态判定失败，降级为纯文本: {e}")
        if resp is None:
            try:
                resp = await _call(pid, [])
            except Exception as e:
                host.logger.error(f"{ctx}[{scene}][模型] 模型调用失败: {e}")
                return None

        raw = getattr(resp, "completion_text", "") or ""
        verdict = moderation.parse_json_loose(raw)
        if not verdict:
            host.logger.warning(f"{ctx}[{scene}][模型] 模型未返回合法 JSON（{len(raw)} 字）: {raw[:200]!r}")
            return None

        # 归一化 + 回填模型信息（时长由代码决定，模型无权指定）
        verdict["violation"] = bool(verdict.get("violation"))
        basis = str(verdict.get("basis") or "").strip()
        if basis not in moderation.ALL_DIMS:
            basis = moderation.DIM_VISION if used_images else moderation.DIM_SEMANTIC
        if used_images and basis in (moderation.DIM_SEMANTIC, moderation.DIM_RULE):
            basis = moderation.DIM_VISION
        verdict["basis"] = basis
        try:
            verdict["confidence"] = float(verdict.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            verdict["confidence"] = 0.0
        code = str(verdict.get("category_code") or "").strip().upper()
        if not categories.valid(code):
            code = ""
        verdict["category_code"] = code
        verdict["category_label"] = categories.label(code)
        verdict["category_desc"] = categories.describe(code)
        verdict["category_public"] = categories.public_text(code)
        verdict["model"] = pid
        verdict["from_cache"] = False
        try:
            dim_name = host.mod.get_dim_names(group).get(basis, basis)
            cat_name = host.mod.get_cat_names(group).get(str(verdict.get("category") or ""), verdict.get("category") or "-")
            host.logger.info(
                f"{ctx}[{scene}][模型] 判定结论 违规={verdict['violation']} 依据={dim_name} 类型={cat_name} "
                f"严重度={verdict.get('severity') or '-'} 置信度={verdict['confidence']:.2f} "
                f"理由={str(verdict.get('reason') or '')[:60]!r}"
            )
        except Exception as e:                                     # 仅日志，绝不因取名失败影响判定
            host.logger.debug(f"{ctx}[{scene}][模型] 判定结论日志输出失败: {e}")
        return verdict
