"""媒体与引用解析。"""

from __future__ import annotations

import json
import time

from ...kernel.text import _looks_like_image_url, _norm_media_url


def _extract_images(host, event):
        """抽取消息里的图片地址（用于多模态审核）。

        三个来源依次兜底，任一条取到就不至于让「图片维度」静默失效：
        1. AstrBot 已解析的 Image 组件（url / file 都可能是 http 地址）；
        2. 原始 payload 里的 attachments —— 适配器版本不同、组件没生成时的兜底；
        3. 官方群消息把「文字 + 图片」拆成两条投递时，由调用方用
           `_nearby_images()` 从相邻缓存补齐（这里只负责单条消息）。
        """
        urls: list[str] = []

        def add(u: str) -> None:
            u = _norm_media_url(u)
            if u.startswith("http") and u not in urls:
                urls.append(u)

        try:
            for c in (getattr(event.message_obj, "message", None) or []):
                if "image" not in type(c).__name__.lower():
                    continue
                for attr in ("url", "file", "path"):
                    add(getattr(c, attr, ""))
        except Exception:
            # noqa: silent-cleanup —— 解析/读取兜底，失败即跳过
            pass

        try:  # 原始附件兜底（图片可能只在 attachments 里，组件里没有）
            raw = getattr(event.message_obj, "raw_message", None)
            for att in (getattr(raw, "attachments", None) or []):
                if isinstance(att, dict):
                    ct = str(att.get("content_type") or "").lower()
                    u = att.get("url") or ""
                else:
                    ct = str(getattr(att, "content_type", "") or "").lower()
                    u = getattr(att, "url", "") or ""
                if ct.startswith("image") or _looks_like_image_url(u):
                    add(u)
        except Exception:
            # noqa: silent-cleanup —— 解析/读取兜底，失败即跳过
            pass
        return urls[:4]

def _find_recent_msg(host, group, text='', ts=0.0, max_age=3600.0):
        """在消息缓存里找被举报的那条（优先按原文，其次转发摘要交集，最后按时间）。

        与撤回用的 _match_recent_msg_id 区别：这里要拿到**整条记录**
        （发送者 openid、图片、真实消息 ID），且时间窗更宽（审核不受 2 分钟限制）。
        """
        try:
            lst = host._recent_group_msgs.get(group) or []
            now = time.time()
            qitems = host._quote_forward_items(text)
            bare = (text or "").strip()
            best, best_score = None, -1.0
            top: list[dict] = []          # 与最高分并列的候选，用来判定「是不是撞了高频短句」
            for r in lst:
                if now - float(r.get("ts") or 0) > max_age:
                    continue
                score = 0.0
                rt = (r.get("text") or "").strip()
                if bare and rt == bare:
                    score += 10.0          # 原文完全一致最强
                elif bare and rt and (bare in rt or rt in bare):
                    score += 4.0
                elif qitems and rt:
                    # 被引用的是合并转发：按「子消息正文行」在缓存正文里的命中比例打分，
                    # 全中 = 8 分（低于原文完全一致，高于普通包含，不会误伤普通消息）。
                    hit = sum(1 for it in qitems if it in rt)
                    if hit:
                        score += 4.0 + 4.0 * hit / len(qitems)
                if ts:
                    score += max(0.0, 3.0 - abs(float(r.get("ts") or 0) - ts))
                if score > best_score:
                    best, best_score = r, score
                    top = [r]
                elif score == best_score and score > 0:
                    top.append(r)
                    # 并列时必须取「最近」那条：缓存是旧→新遍历，
                    # 只写 score > best_score 会让同文消息永远命中最早的一条（发送者就错了）。
                    if float(r.get("ts") or 0) > float(best.get("ts") or 0):
                        best = r
            if best is not None:
                senders = {str(r.get("sender") or "") for r in top}
                if len(senders) > 1:
                    host.logger.warning(
                        f"[审核] 引用目标存在 {len(top)} 条同分候选（{len(senders)} 个发送者），"
                        f"已按最近时间取 id={best.get('id')}；若判错请让当事人重新引用一次"
                    )
                    best = dict(best)
                    best["match_ambiguous"] = True
                host.logger.debug(
                    f"审核引用命中缓存: items={qitems[:3]} score={best_score:.1f} "
                    f"id={best.get('id')} text_len={len(best.get('text') or '')} "
                    f"candidates={len(top)}"
                )
            return best if best_score > 0 else None
        except Exception:
            return None

def _nearby_images(host, group, sender, anchor_ts, window=3.0, limit=4):
        """从相邻缓存里补图片。

        官方群消息里「文字 + 图片」经常拆成两条投递：文本那条有正文、图片那条正文为空。
        举报按正文匹配到的是文本那条，图片得从同一发送者、前后几秒的缓存里捞回来——
        否则图片维度会被静默跳过（判定日志里只剩「模式=纯文本」）。
        """
        urls: list[str] = []
        if not anchor_ts:
            return urls
        for r in reversed(host._recent_group_msgs.get(group) or []):
            if sender and str(r.get("sender") or "") != str(sender):
                continue
            if abs(float(r.get("ts") or 0) - float(anchor_ts)) > window:
                continue
            for u in (r.get("images") or []):
                if u not in urls:
                    urls.append(u)
            if len(urls) >= limit:
                break
        return urls[:limit]

def _latest_image_msg(host, group, max_age=120.0):
        """最近一条带图片的缓存消息。

        引用的是**纯图片消息**时，引用摘要里没有任何正文，按正文匹配必定失败；
        这种情况按「最近的图片消息」兜底，总比直接把图片维度丢掉好。
        """
        now = time.time()
        for r in reversed(host._recent_group_msgs.get(group) or []):
            if now - float(r.get("ts") or 0) > max_age:
                continue
            if r.get("images"):
                return r
        return None

def _quote_attachments_from_raw(host, raw):
        """从引用事件原始 payload 的 msg_elements[0].attachments 里挖被引用图片。

        AstrBot 会把 quoted attachments 塞进 Reply.chain，但部分版本/部分客户端
        的引用事件只给文本摘要，这里直接再读一遍原始字段做兜底。
        字段可能是 dict 也可能是对象，两种都兼容。
        """
        out: list[str] = []
        try:
            elems = getattr(raw, "msg_elements", None) or []
            if not elems:
                return out
            e0 = elems[0]
            atts = e0.get("attachments") if isinstance(e0, dict) else getattr(e0, "attachments", None)
            for att in (atts or []):
                if isinstance(att, dict):
                    ct = str(att.get("content_type") or "").lower()
                    u = att.get("url") or ""
                else:
                    ct = str(getattr(att, "content_type", "") or "").lower()
                    u = getattr(att, "url", "") or ""
                if not u:
                    continue
                if ct.startswith("image") or _looks_like_image_url(u):
                    u = _norm_media_url(u)
                    if u not in out:
                        out.append(u)
        except Exception:
            # noqa: silent-cleanup —— 解析/读取兜底，失败即跳过
            pass
        return out[:4]
