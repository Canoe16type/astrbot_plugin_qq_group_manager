"""词库相关 Web API（路径与响应格式保持不变）。"""

from __future__ import annotations

import re

from astrbot.api.web import json_response, request

try:
    from .... import moderation
except ImportError:  # 测试把插件目录直接加入 sys.path 时
    import moderation  # type: ignore


async def web_lexicons(host):
        if request.query.get("refresh"):
            host.lexicons.scan(force=True)
        return json_response({
            "stats": host.lexicons.stats(),
            "lexicons": [host._lex_payload(x) for x in host.lexicons.list_lexicons()],
            "allow": sorted(host.lexicons.allow_words()),
            "manual_allow": list(host.lexicons.state.get("allow") or []),
            "categories": [{"key": k, "name": v} for k, v in moderation.DEFAULT_CAT_NAMES.items()],
            "upstream": host._lexicon_upstream_conf(),
        })

async def web_lexicon_detail(host):
        lid = str(request.query.get("id") or "")
        item = host.lexicons.get(lid)
        if not item:
            return json_response({"error": "词库不存在"})
        d = host._lex_payload(item)
        d["enabled"] = host.lexicons.is_enabled(item)
        # 词条内容不下发展示：只给统计（每类条数 / 长度分布），避免在界面上把敏感词摊开
        literals = item.get("literals") or {}
        regexes = item.get("regexes") or {}
        dist: dict[str, int] = {}
        for words in literals.values():
            for w in words:
                k = str(len(w))
                dist[k] = dist.get(k, 0) + 1
        d["word_display"] = "hidden"
        d["shown"] = False
        d["length_dist"] = dict(sorted(dist.items(), key=lambda kv: int(kv[0])))
        d["regex_count"] = sum(len(v) for v in regexes.values())
        d["regex_categories"] = sorted(regexes.keys())
        return json_response({"lexicon": d})

async def web_lexicon_export(host):
        return json_response(host.lexicons.export(str(request.query.get("id") or "")))

async def web_lexicon_rename(host):
        p = await request.json(default={})
        r = host.lexicons.rename(str(p.get("id") or ""), str(p.get("name") or ""))
        return json_response(r)

async def web_lexicon_delete(host):
        p = await request.json(default={})
        r = host.lexicons.delete(str(p.get("id") or ""))
        if r.get("ok"):
            host.logger.info(f"[词库] 删除 {r.get('id')}")
        return json_response(r)

async def web_lexicon_apply(host):
        p = await request.json(default={})
        ids = p.get("ids")
        if not isinstance(ids, list):
            return json_response({"error": "ids 必须是数组"})
        stats = host.lexicons.apply([str(x) for x in ids])
        host._variant_sync(force=True)
        return json_response({"ok": True, "stats": stats})

async def web_lexicon_toggle(host):
        p = await request.json(default={})
        lid = str(p.get("id") or "")
        if not host.lexicons.get(lid):
            return json_response({"error": "词库不存在"})
        st = host.lexicons.set_enabled(lid, bool(p.get("enabled")))
        host._variant_sync(force=True)     # 词库变了，变体索引必须跟着重建，否则停用形同虚设
        host.logger.info(f"[词库] {lid} -> {'启用' if p.get('enabled') else '停用'}")
        return json_response({"ok": True, "stats": st})

async def web_lexicon_fetch_upstream(host):
        p = await request.json(default={})
        r = await host._fetch_upstream_lexicon(
            url=str(p.get("url") or ""),
            name=str(p.get("name") or ""),
            category=str(p.get("category") or ""),
            auto_enable=None if p.get("auto_enable") is None else bool(p.get("auto_enable")),
            mirror=None if p.get("mirror") is None else str(p.get("mirror") or ""),
            force_mirror=None if p.get("force_mirror") is None else bool(p.get("force_mirror")),
        )
        return json_response(r)

async def web_lexicon_upload(host):
        p = await request.json(default={})
        r = host.lexicons.upload(
            name=str(p.get("name") or ""),
            content=str(p.get("content") or ""),
            fmt=str(p.get("format") or ""),
            default_category=str(p.get("category") or "other"),
            description=str(p.get("description") or ""),
            mode=str(p.get("mode") or "new"),
            target_id=str(p.get("target_id") or ""),
            match_mode=str(p.get("match") or "auto"),
        )
        if r.get("ok"):
            host.logger.info(f"[词库] 上传「{r.get('name')}」{r.get('count')} 条（{r.get('mode')}，{r.get('id')}）")
        return json_response(r)

async def web_lexicon_allow(host):
        p = await request.json(default={})
        action = str(p.get("action") or "add").lower()
        if action == "clear":
            host.logger.info("[词库] 清空白名单")
            return json_response(host.lexicons.allow_clear())
        raw = p.get("words")
        if isinstance(raw, str):
            items = [x.strip() for x in re.split(r"[\n,，、;；]+", raw) if x.strip()]
        elif isinstance(raw, list):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            items = []
        if not items:
            return json_response({"error": "没有有效的词"})
        if action == "remove":
            r = host.lexicons.allow_remove(items)
            host.logger.info(f"[词库] 白名单移除 {len(r.get('removed') or [])} 条")
            return json_response(r)
        r = host.lexicons.allow_add(items)
        host.logger.info(f"[词库] 白名单新增 {len(r.get('added') or [])} 条")
        return json_response(r)

async def web_lexicon_test(host):
        p = await request.json(default={})
        text = str(p.get("text") or "")
        include_disabled = bool(p.get("include_disabled"))
        use_allow = p.get("use_allow", True) is not False
        hits = host.lexicons.hits(text, limit=20, include_disabled=include_disabled, use_allow=use_allow)
        allow_words = sorted(host.lexicons.allow_words()) if use_allow else []
        blocked_count = 0
        if use_allow and text:
            plain = host.lexicons.hits(text, limit=20, include_disabled=include_disabled, use_allow=False)
            blocked_count = len([h for h in plain if h["match"] in set(allow_words)])

        def _anon(h: dict) -> dict:
            """命中信息去词化：只报位置、词长、类型、来源，不回传词本身。"""
            return {
                "category": h.get("category"),
                "source": h.get("source"),
                "start": h.get("start"),
                "end": h.get("end"),
                "length": len(str(h.get("match") or "")),
                "is_regex": bool(h.get("is_regex")),
            }

        return json_response({
            "text": text,
            "count": len(hits),
            "hits": [_anon(h) for h in hits],
            "blocked_count": blocked_count,
            "word_display": "hidden",
            "stats": host.lexicons.stats(),
        })
