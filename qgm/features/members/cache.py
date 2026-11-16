"""最近消息缓存与成员事件去重。"""

from __future__ import annotations

import json
import os
import time

from ...kernel.timefmt import _active_tz

try:
    from .... import member_event
except ImportError:
    import member_event  # type: ignore


def _load_recent_msgs(host):
        """启动时恢复最近消息缓存（只保留 10 分钟内），让重载前的消息也能被举报定位。"""
        try:
            with open(host._recent_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            host.logger.debug(f"[审核] 最近消息缓存读取失败（忽略）：{e}")
            return
        now = time.time()
        try:
            for g, lst in (data or {}).items():
                keep = [r for r in lst if isinstance(r, dict) and now - float(r.get("ts") or 0) <= 600]
                if keep:
                    host._recent_group_msgs[str(g)] = keep[-200:]
            total = sum(len(v) for v in host._recent_group_msgs.values())
            if total:
                host.logger.debug(f"[审核] 最近消息缓存已恢复：{total} 条 / {len(host._recent_group_msgs)} 个群")
        except Exception as e:
            host.logger.debug(f"[审核] 最近消息缓存解析失败（忽略）：{e}")

def _save_recent_msgs(host, force=False):
        """节流落盘（默认 3 秒一次），避免每条消息都写文件。"""
        now = time.time()
        if not force and now - host._recent_saved_at < 3:
            return
        try:
            data = {
                g: [r for r in lst if now - float(r.get("ts") or 0) <= 600][-200:]
                for g, lst in host._recent_group_msgs.items()
            }
            tmp = host._recent_cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, host._recent_cache_path)
            host._recent_saved_at = now
        except Exception as e:
            host.logger.debug(f"[审核] 最近消息缓存写入失败（忽略）：{e}")

def _record_recent_msg(host, event):
        """缓存当前群消息（原文 + 消息 ID + 时间），供引用撤回按原文匹配。

        只缓存普通消息（message_type != 103）；引用消息（103）自身不缓存，
        避免干扰匹配。每群最多保留 200 条 / 10 分钟内的消息。
        """
        try:
            g = event.get_group_id()
            if not g:
                return
            host._last_active_group = g
            msg_id = str(getattr(event.message_obj, "message_id", "") or "")
            if not msg_id:
                return
            raw = getattr(event.message_obj, "raw_message", None)
            mt = None
            if raw is not None:
                if isinstance(raw, dict):
                    mt = raw.get("message_type")
                else:
                    mt = getattr(raw, "message_type", None)
            try:
                if mt is not None and int(mt) == 103:
                    return  # 引用消息自身不入缓存
            except (TypeError, ValueError):
                # noqa: silent-cleanup —— 降级兜底，不影响主流程
                pass
            text = host._extract_text(event)[:500]   # 落盘缓存只留前 500 字
            images = host._extract_images(event)
            if not text and not images:
                return
            lst = host._recent_group_msgs.setdefault(g, [])
            lst.append({
                "id": msg_id,
                "text": text,
                "ts": time.time(),
                "sender": event.get_sender_id() or "",
                "sender_name": event.get_sender_name() or "",
                # 图片地址：仅本地留存，供 /举报 的多模态审核取用（QQ 图片
                # URL 会过期，举报稍晚就可能取不到，所以必须提前存下来）。
                "images": images,
            })
            # 裁剪：最多 200 条，且丢弃超过 10 分钟的旧消息
            now = time.time()
            lst[:] = [r for r in lst if now - r["ts"] <= 600][-200:]
            host._save_recent_msgs()
        except Exception:
            # noqa: silent-cleanup —— 降级兜底，不影响主流程
            pass

def _match_recent_msg_id(host, group_openid, text, max_age=150.0):
        """在消息缓存中按原文查找最近的消息 ID（用于引用撤回）。

        撤回时限 2 分钟，默认只匹配 150 秒内、原文完全一致的最近一条。
        返回消息 ID，未命中返回空串。
        """
        try:
            lst = host._recent_group_msgs.get(group_openid) or []
            now = time.time()
            best = ""
            best_ts = 0.0
            for r in lst:
                if now - r["ts"] > max_age:
                    continue
                if (r.get("text") or "").strip() == (text or "").strip():
                    if r["ts"] >= best_ts:
                        best_ts = r["ts"]
                        best = str(r.get("id") or "")
            return best
        except Exception:
            return ""

def _record_member_seen(host, event):
        """普通群消息：把发言人记进档案。

        官方群消息事件的 author 里只有 member_openid，昵称通常是空的；
        这里只有在适配器真给了昵称时才顺手填（source=msg，人工昵称不会被覆盖）。
        """
        try:
            if not host._members_on():
                return
            if not bool(host._read_config_value(("member_registry", "record_on_message"), default=True)):
                return
            g = event.get_group_id()
            sid = str(event.get_sender_id() or "")
            if not g or not sid or not host._group_in_scope(g, event.get_platform_id()):
                return
            sname = str(event.get_sender_name() or "").strip()
            name = sname if (sname and sname != sid) else ""
            host.members.seen(g, sid, name=name)
        except Exception as e:  # noqa: BLE001
            host.logger.debug(f"[成员档案] 记录发言失败: {e}")

def _member_event_seen(host, event_id):
        """退群/入群事件去重：平台重投或重连补发时，同一事件只处理一次。"""
        cache = getattr(host, "_member_events_seen", None)
        if cache is None:
            cache = host._member_events_seen = {}
        now = time.time()
        for k, ts in list(cache.items()):
            if now - ts > 300:          # 5 分钟窗口足够覆盖重投
                cache.pop(k, None)
        if event_id in cache:
            return True
        cache[event_id] = now
        if len(cache) > 500:
            for k, _ in sorted(cache.items(), key=lambda kv: kv[1])[: len(cache) - 500]:
                cache.pop(k, None)
        return False

def _ensure_member_event_injected(host):
        """确保群成员事件注入完成；失败时延迟重试。

        解决两个问题：
        1. 插件 initialize 时平台实例可能尚未就绪（AstrBot 启动顺序），
           此时注入会失败，等第一条群消息进来时平台必然已就绪，可重试；
        2. 多实例配置下 get_platform_inst("qq_official") 按实例 ID 匹配
           必然返回 None（"qq_official" 只是平台名），必须遍历平台实例注入。
        重试间隔 300 秒，避免反复触发 websocket 重连。
        """
        if host._member_event_ok:
            return
        now = time.time()
        if now - host._last_inject_attempt < 300:
            return
        host._last_inject_attempt = now
        try:
            n = member_event.install_member_event_support(host, host.context)
            if n > 0:
                host._member_event_ok = True
                host.logger.info(f"群成员事件注入成功（{n} 个 qq_official 实例）")
        except Exception as e:
            host.logger.warning(f"群成员事件注入失败（不影响其他功能）: {e}")

async def _refresh_bot_name(host, platform_id, ttl=120.0):
        """拉机器人自己的昵称（GET /users/@me，普通接口），失败返回空串。

        - 异常一律吞掉：昵称只是显示用，不能因为它把「策略列表」整页搞崩。
        - 带 TTL 节流：拉不到时别把每个列表请求都拖成两次 API 往返。
        """
        tried = getattr(host, "_bot_name_tried", None)
        if tried is None:
            tried = host._bot_name_tried = {}
        now = time.monotonic()
        if now - tried.get(platform_id, 0.0) < ttl:
            return str((host.mod.get_bot_meta(platform_id) or {}).get("name") or "")
        tried[platform_id] = now
        try:
            api = host._get_api(platform_id)
            info = await api.get_bot_info()
            name = str((info or {}).get("username") or "").strip()
            if name:
                host.mod.set_bot_meta(platform_id, name=name[:40])
            return name
        except Exception as e:  # noqa: BLE001 昵称拿不到不影响主流程
            host.logger.debug(f"[入群审批] 拉取机器人昵称失败 {platform_id}: {e}")
            return ""
