"""举报目标与引用解析（机械搬迁，同步实现）。"""

from __future__ import annotations

import time

from astrbot.api.message_components import Reply

from ...kernel.text import _norm_media_url


def _mod_resolve_reported(host, event):
        """解析「被举报的消息」。

        沿用插件里已验证的引用解析链路（与 /撤回 同源）：
        Reply 组件原文 -> botpy 原始 payload -> 消息缓存按原文匹配真实消息 ID。
        返回 dict：quoted=是否带了引用，resolved=是否已定位到发送者。
        """
        out = {
            "quoted": False, "resolved": False, "id": "", "text": "",
            "sender": "", "sender_name": "", "images": [], "ts": time.time(),
        }
        g = event.get_group_id()
        if not g:
            return out
        message_id = ""
        quoted_content = ""
        quoted_images = []
        try:

            reply = next((c for c in (event.message_obj.message or []) if isinstance(c, Reply)), None)
            if reply is not None:
                out["quoted"] = True
                rid = getattr(reply, "id", None)
                if rid:
                    message_id = str(rid)
                quoted_content = host._clean_quote_summary(getattr(reply, "message_str", "") or "")
                # reply.chain 里的文本才是被引用原文；message_str 有时只剩占位标题。
                chain_parts = []
                for sub in (getattr(reply, "chain", None) or []):
                    if type(sub).__name__ == "Image":
                        u = _norm_media_url(getattr(sub, "url", "") or getattr(sub, "file", "") or "")
                        if u.startswith("http") and u not in quoted_images:
                            quoted_images.append(u)
                    else:
                        t = str(getattr(sub, "text", "") or "")
                        if t.strip():
                            chain_parts.append(t.strip())
                if chain_parts:
                    quoted_content = host._clean_quote_summary("\n".join(chain_parts))
        except Exception:
            pass
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw is not None:
                mid, mcontent = host._extract_quote_from_raw(raw)
                if mid:
                    message_id = message_id or str(mid)
                    out["quoted"] = True
                if mcontent and not quoted_content:
                    quoted_content = str(mcontent)
                    out["quoted"] = True
                # 引用图片：适配器没转成 Image 组件时，直接读原始 attachments
                for u in host._quote_attachments_from_raw(raw):
                    if u not in quoted_images:
                        quoted_images.append(u)
        except Exception:
            pass

        rec = None
        if quoted_content:
            rec = host._find_recent_msg(g, text=quoted_content)
        if rec is None and message_id:
            for r in (host._recent_group_msgs.get(g) or []):
                if str(r.get("id") or "") == message_id:
                    rec = r
                    break
        if rec is None and not quoted_content.strip():
            # 引用的是纯图片消息：摘要里没有任何正文可匹配，按最近的图片消息兜底
            rec = host._latest_image_msg(g)
            if rec is not None:
                host.logger.info(
                    f"{host._ctx('', g)}[审核] 引用的是纯图片消息，按最近缓存图片兜底："
                    f"id={rec.get('id')} ts={rec.get('ts')}"
                )
        if rec is None:
            # 兜底：被举报的是合并转发时，摘要里的 [消息内容] 行至少能拿去审核，
            # 别把「[消息类型] 引用消息」这类噪声丢给模型。
            items = host._quote_forward_items(quoted_content)
            out["text"] = "\n".join(items) if items else quoted_content
            out["images"] = quoted_images
            # 内容有、发送者没定位到（引用的是 10 分钟以前的消息）：
            # 不冒充已定位，但也不放弃审核——交给上层走「只判定不处罚」。
            if out["text"].strip() or out["images"]:
                out["unattributed"] = True
            host.logger.debug(
                f"{host._ctx('', g)}[审核] 引用未命中缓存: 摘要长度={len(quoted_content)} items={items[:3]} "
                f"unattributed={bool(out.get('unattributed'))}"
            )
            return out
        real_id = str(rec.get("id") or "")
        # QQ 官方引用事件只给 REFIDX 引用索引，撤回 API 认的是缓存里的真实消息 ID
        if real_id and (not message_id or message_id.startswith(("REFIDX", "TMP_"))):
            message_id = real_id
        # 命中缓存时优先用缓存里的正文：合并转发的缓存正文是完整展开版，
        # 比引用摘要（夹着 [消息类型] 等元信息）更适合做审核判定。
        images = list(rec.get("images") or []) or list(quoted_images)
        if not images:
            # 「文字 + 图片」被拆成两条投递时，图片在那条正文为空的相邻消息里，
            # 按同发送者 ±3s 补齐；补不上就只能走纯文本，别假装有图。
            images = host._nearby_images(g, str(rec.get("sender") or ""), float(rec.get("ts") or 0))
            if images:
                host.logger.info(
                    f"{host._ctx('', g, str(rec.get('sender') or ''))}[审核] 被举报消息本身无图，按相邻缓存补齐 {len(images)} 张"
                )
        out.update({
            "resolved": True,
            "match_ambiguous": bool(rec.get("match_ambiguous")),
            "id": message_id or real_id,
            "text": str(rec.get("text") or "") or quoted_content,
            "sender": str(rec.get("sender") or ""),
            "sender_name": str(rec.get("sender_name") or ""),
            "images": images[:4],
            "ts": float(rec.get("ts") or time.time()),
        })
        return out

def _extract_quote_from_raw(host, raw):
        """从 botpy 原始消息对象中提取被引用消息的 ID 与原文。

        QQ 官方群消息引用事件（message_type=103）结构：
        - msg_elements[0].msg_idx: 被引用消息 ID（REFIDX_xxx== 格式）
        - msg_elements[0].content: 被引用消息原文
        - message_scene.ext: ["msg_idx=...", "ref_msg_idx=...", ...]
        - message_reference.message_id: 部分场景下直接携带

        返回 (message_id, content)，取不到时为空字符串；解析异常不抛出。
        """
        mid = ""
        content = ""
        try:
            elems = getattr(raw, "msg_elements", None) or []
            scene = getattr(raw, "message_scene", None) or {}
            ref = getattr(raw, "message_reference", None) or {}

            # 调试：完整 dump 引用事件的关键字段，确认真实 payload 结构
            try:
                import json as _json

                def _d(v):
                    if isinstance(v, (dict, list)):
                        try:
                            return _json.dumps(v, ensure_ascii=False)
                        except Exception:
                            return str(v)
                    return str(v)

                host.logger.debug(
                    "引用消息原始结构: "
                    f"id={getattr(raw, 'id', '')!r} "
                    f"message_type={getattr(raw, 'message_type', '')!r} "
                    f"msg_elements={_d(elems)} "
                    f"message_scene={_d(scene)} "
                    f"message_reference={_d(ref)}"
                )
            except Exception:
                pass

            # 1) 引用元素里直接带被引用消息 ID（REFIDX 格式）与原文
            if elems:
                e0 = elems[0]
                if isinstance(e0, dict):
                    mid = str(
                        e0.get("msg_idx") or e0.get("id") or e0.get("message_id") or ""
                    )
                    content = host._clean_quote_summary(str(e0.get("content") or ""))
                else:
                    mid = str(
                        getattr(e0, "msg_idx", "")
                        or getattr(e0, "id", "")
                        or getattr(e0, "message_id", "")
                    )
                    content = host._clean_quote_summary(str(getattr(e0, "content", "") or ""))
            # 2) message_reference 直接携带被引用消息 ID
            if not mid:
                if isinstance(ref, dict):
                    mid = str(ref.get("message_id") or "")
                else:
                    mid = str(getattr(ref, "message_id", "") or "")
            # 3) message_scene.ext 里找 ref_msg_idx / msg_idx
            if not mid:
                if isinstance(scene, dict):
                    ext = scene.get("ext", []) or []
                else:
                    ext = getattr(scene, "ext", None) or []
                for kv in ext:
                    if isinstance(kv, str) and kv.startswith("ref_msg_idx="):
                        mid = kv.split("=", 1)[1]
                        break
                if not mid:
                    for kv in ext:
                        if isinstance(kv, str) and kv.startswith("msg_idx="):
                            mid = kv.split("=", 1)[1]
                            break
        except Exception as e:
            host.logger.debug(f"解析引用消息原始结构失败: {e}")
        return mid, content
