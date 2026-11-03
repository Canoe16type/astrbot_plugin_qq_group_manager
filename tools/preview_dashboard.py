"""面板本地预览（开发用）：不启动 AstrBot，也能在浏览器里点真面板。

为什么要有这个
--------------
面板前端跑在 AstrBot WebUI 的沙箱 iframe 里（sandbox="allow-scripts allow-forms
allow-downloads"，**没有 allow-same-origin / allow-modals**）。这带来两个后果：

1. 原生 alert / confirm / prompt 会被浏览器直接拦掉，点了像没反应；
2. 只能真机点才知道，静态检查、编译检查一律抓不到。

所以本脚本起一个假后端：把 pages/dashboard/ 当静态站发出去，注入一个假的
`window.AstrBotPluginPage`（apiGet / apiPost），接口一律返回内置的假数据。
配合 Playwright 就能在 CI 或本地真点按钮、写真机截图。

用法
----
    python tools/preview_dashboard.py                # http://127.0.0.1:8899
    python tools/preview_dashboard.py --port 9000
    python tools/preview_dashboard.py --members 30   # 假数据条数

注意：假数据只用于看界面，不写任何真实插件数据。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = os.path.join(ROOT, "pages", "dashboard")

GROUP = "C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6"


def _fake_members(n: int) -> list[dict]:
    now = time.time()
    rows = []
    for i in range(n):
        left = i % 5 == 4
        rows.append({
            "openid": f"{GROUP[:20]}{i:04d}",
            "user_openid": "",
            "nickname": "" if i % 3 == 2 else f"成员{i + 1:02d}",
            "name_source": ["manual", "api", "none", "msg"][i % 4],
            "note": "" if i % 2 else "活跃老群友",
            "role": "",
            "first_seen": now - 3600 * (i + 1),
            "last_seen": 0.0 if left else now - 600 * i,
            "msg_count": 0 if left else (i * 3) % 40,
            "status": "left" if left else "active",
            "left_at": now - 300 if left else 0.0,
            "join_count": 1 + (i % 3),
        })
    return rows


MEMBERS = _fake_members(12)


def _members_payload(q: dict) -> dict:
    status = (q.get("status") or ["all"])[0]
    sort = (q.get("sort") or ["last_seen"])[0]
    kw = (q.get("keyword") or [""])[0].strip().lower()
    page = int((q.get("page") or ["1"])[0] or 1)
    size = int((q.get("size") or ["30"])[0] or 30)

    rows = list(MEMBERS)
    if status == "active":
        rows = [r for r in rows if r["status"] != "left"]
    elif status == "left":
        rows = [r for r in rows if r["status"] == "left"]
    elif status == "named":
        rows = [r for r in rows if r["nickname"]]
    elif status == "unnamed":
        rows = [r for r in rows if not r["nickname"]]
    if kw:
        rows = [r for r in rows if kw in (r["nickname"] + r["note"] + r["openid"]).lower()]

    keys = {
        "last_seen": lambda r: (r["status"] == "left", -r["last_seen"]),
        "first_seen": lambda r: (r["status"] == "left", r["first_seen"]),
        "msg_count": lambda r: (r["status"] == "left", -r["msg_count"]),
        "name": lambda r: r["nickname"] or "\uffff",
    }
    rows.sort(key=keys.get(sort, keys["last_seen"]))
    total = len(rows)
    chunk = rows[(page - 1) * size: page * size]
    for r in chunk:
        r["last_seen_text"] = "" if not r["last_seen"] else time.strftime(
            "%m-%d %H:%M", time.localtime(r["last_seen"]))
    return {
        "items": chunk,
        "total": total,
        "page": page,
        "size": size,
        "stats": {
            "total": len(MEMBERS),
            "active": sum(1 for r in MEMBERS if r["status"] != "left"),
            "left": sum(1 for r in MEMBERS if r["status"] == "left"),
            "named": sum(1 for r in MEMBERS if r["nickname"]),
            "unnamed": sum(1 for r in MEMBERS if not r["nickname"]),
            "messages": sum(r["msg_count"] for r in MEMBERS),
        },
    }


def api_get(path: str, q: dict):
    if path == "mod/groups":
        return {"bots": [{"name": "测试实例", "groups": [
            {"group": GROUP, "display": "测试（小16 | No.1）"}]}]}
    if path == "members":
        return _members_payload(q)
    if path == "status":
        return {"platform": "qq_official", "version": "v1.1.1-preview"}
    if path == "config/schema":
        groups = []
        for gi, (gkey, gname) in enumerate((("base", "基础功能"), ("perm", "权限判定"),
                                            ("recall", "撤回功能"), ("join", "入群审批"))):
            items = [{
                "key": f"{gkey}_item{i}",
                "description": f"{gname} · 示例配置项 {i}",
                "type": "bool" if i % 3 == 0 else ("int" if i % 3 == 1 else "str"),
                "value": True if i % 3 == 0 else (600 if i % 3 == 1 else "示例文案"),
                "default": True if i % 3 == 0 else (600 if i % 3 == 1 else "示例文案"),
                "hint": "这是预览用的假配置项，仅用于检查排版宽度",
            } for i in range(4)]
            groups.append({"key": gkey, "name": gname, "icon": "🌱", "items": items})
        return {"groups": groups, "providers": [], "reload_required": {}}
    if path == "config":
        return {"config": {}}
    if path == "history":
        return {"items": [], "total": 0}
    if path == "logs":
        return {"lines": []}
    return {"items": [], "total": 0, "bots": []}


def api_post(path: str, body: dict):
    if path == "members/sync":
        return {"count": 0, "added": 0, "updated": 0,
                "message": "接口返回 11253：群成员列表为内邀白名单能力"}
    if path == "members/update":
        for r in MEMBERS:
            if r["openid"] == body.get("openid"):
                if "nickname" in body:
                    r["nickname"] = str(body["nickname"] or "")
                if "note" in body:
                    r["note"] = str(body["note"] or "")
        return {"ok": True}
    if path == "members/delete":
        MEMBERS[:] = [r for r in MEMBERS if r["openid"] != body.get("openid")]
        return {"ok": True}
    return {"ok": True}


SHIM = """<script>
(function(){
  function url(path, params){
    var qs = new URLSearchParams(params || {}).toString();
    return "/api/" + path + (qs ? "?" + qs : "");
  }
  window.AstrBotPluginPage = {
    apiGet: function(path, params){
      return fetch(url(path, params)).then(function(r){ return r.json(); });
    },
    apiPost: function(path, body){
      return fetch("/api/" + path, {method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body || {})}).then(function(r){ return r.json(); });
    }
  };
})();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        return

    def _send(self, code: int, data: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/"):
            return self._json(api_get(u.path[5:], q))
        rel = u.path.lstrip("/") or "index.html"
        fp = os.path.normpath(os.path.join(PAGES, rel))
        if not fp.startswith(PAGES) or not os.path.isfile(fp):
            return self._send(404, b"not found", "text/plain")
        with open(fp, "rb") as f:
            data = f.read()
        if fp.endswith("index.html"):
            html = data.decode("utf-8")
            html = html.replace("</head>", SHIM + "</head>", 1)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        return self._send(200, data, ctype)

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            body = {}
        if u.path.startswith("/api/"):
            return self._json(api_post(u.path[5:], body))
        return self._send(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--members", type=int, default=12, help="假数据条数")
    args = ap.parse_args()
    MEMBERS[:] = _fake_members(max(0, args.members))
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"预览： http://127.0.0.1:{args.port}/   (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
