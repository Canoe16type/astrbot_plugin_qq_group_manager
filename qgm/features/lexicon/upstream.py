"""上游词库下载。"""

from __future__ import annotations

import re
import time
import urllib.request

try:
    from .... import lexicon as lexicon_lib, moderation
except ImportError:
    import lexicon as lexicon_lib  # type: ignore
    import moderation  # type: ignore

from ...kernel.constants import LEXICON_UPSTREAM_NAME


async def fetch_upstream_lexicon(host, url: str = "", name: str = "", category: str = "",
                                 auto_enable=None, mirror: str = "", force_mirror=None):
        """把上游词表下载下来写成本地词库：同名则覆盖更新，避免每点一次多一个库。"""
        conf = host._lexicon_upstream_conf()
        url = (url or conf["url"]).strip()
        cat = (category or conf["category"]).strip()
        if cat not in moderation.DEFAULT_CAT_NAMES:
            cat = "other"
        auto = conf["auto_enable"] if auto_enable is None else bool(auto_enable)
        use_mirror = conf["mirror"] if mirror is None else str(mirror or "").strip()
        force = conf["force_mirror"] if force_mirror is None else bool(force_mirror)
        name = (name or LEXICON_UPSTREAM_NAME).strip()[:40] or LEXICON_UPSTREAM_NAME
        started = time.time()

        if not url:
            return {"ok": False, "url": "",
                    "error": "还没填词表地址：插件不内置任何下载链接，请自行找一份词表填进来，或直接上传文件"}
        if not re.match(r"^https?://", url, re.I):
            return {"ok": False, "error": "只支持 http/https 地址", "url": url}
        if "@" in urllib.parse.urlsplit(url).netloc:
            return {"ok": False, "error": "地址里不要带用户名密码", "url": url}
        if force and not use_mirror:
            return {"ok": False, "error": "勾了「只走加速」但没填加速地址", "url": url}

        text, err, source, used_url = await host._download_upstream(url, use_mirror, force)
        if err:
            host.logger.warning(f"[词库] 一键获取失败：{err}（{url}）")
            return {"ok": False, "error": err, "url": url, "mirror": use_mirror}

        exist = next(
            (x for x in host.lexicons.list_lexicons()
             if x.get("source") != "builtin" and x.get("name") == name),
            None,
        )
        prev_count = int(exist.get("count") or 0) if exist else 0
        via = "加速" if source == "mirror" else "直连"
        r = host.lexicons.upload(
            name=name, content=text, fmt="txt", default_category=cat,
            description=f"一键获取自动生成（{via}），来源：{url}"[:300],
            mode="overwrite" if exist else "new",
            target_id=exist["id"] if exist else "",
            match_mode=lexicon_lib.MATCH_LITERAL,
        )
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "写入词库失败", "url": url}
        # upload 新建时默认启用，这里按设置改回用户要的状态
        host.lexicons.set_enabled(r["id"], auto)
        count = int(r.get("count") or 0)
        host.logger.info(
            f"[词库] 一键获取「{r.get('name')}」{count} 条"
            f"（{'覆盖' if exist else '新建'}·{via}，{'已启用' if auto else '未启用'}）"
        )
        # 走加速站时词条数剧烈变化，多半是镜像给了错误页/别的版本，提醒一句
        note = ""
        if source == "mirror":
            note = "本次走的是第三方加速地址，建议核对一下条数是否合理"
        return {
            "ok": True, "url": url, "used_url": used_url, "source": via,
            "id": r["id"], "name": r.get("name"), "count": count,
            "prev_count": prev_count,
            "mode": "overwrite" if exist else "new", "enabled": auto,
            "errors": (r.get("errors") or [])[:5],
            "warnings": (r.get("warnings") or [])[:5],
            "note": note,
            "elapsed_ms": int((time.time() - started) * 1000),
        }
