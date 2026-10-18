#!/usr/bin/env python3
"""「/管理登记」绑定管理员的指令链路自测（离线，不需要 AstrBot 实例）。

跑法:
    python3 tools/selftest_admin_register.py

覆盖：
  1. 私聊（无群）→ 明确拒绝，不静默
  2. 群未放行（basic.target_group_openids 不含本群）→ 静默不回复、不写名单
  3. 非管理员 → 走默认话术 msg_not_admin，且不写名单
  4. 管理员 /管理登记 → 写入名单 + 成功话术（带人数）+ 记录角色来源
  5. 重复登记 → 幂等，人数不涨
  6. /管理登记 列表 → 列出名单
  7. 不带斜杠「管理登记」：@ 机器人时生效；没 @ 时不误触发
  8. 关掉「变体累计提醒」后登记 → 登记成功 + 追加提示 mod_admin_register_off
  9. /管理登记 取消 → 移出名单
 10. 没登记过的人 取消 → 明确告知未登记

设计口径（与 main.py 注释一致）：QQ 官方通道拿不到稳定的群角色字段，
所以「谁兜底」由管理员主动登记；本群没人登记 → 变体累计提醒不发。
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(BASE))

from _plugin_import import load  # noqa: E402

moderation = load("moderation")

FAILED: list[str] = []
SKIPPED: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILED.append(msg)


def skip(msg: str) -> None:
    print("  ⏭ 跳过：" + msg)
    SKIPPED.append(msg)


def find_astrbot_site() -> str:
    """定位 AstrBot 站点包目录（可移植）。

    顺序：已能 import astrbot → 环境变量 ASTRBOT_SITE → 当前 sys.path →
    本机 site-packages → 常见虚拟环境路径。找不到返回空串，调用方自行跳过。
    """
    try:
        import astrbot  # noqa: F401

        return os.path.dirname(os.path.dirname(os.path.abspath(astrbot.__file__)))
    except Exception:
        pass
    env = os.environ.get("ASTRBOT_SITE", "").strip()
    cands = [env] if env else []
    cands += [p for p in sys.path if p]
    try:
        import site as _site

        cands += list(_site.getsitepackages())
        cands.append(_site.getusersitepackages())
    except Exception:
        pass
    cands += [
        os.path.expanduser("~/.local/share/uv/tools/*/lib/python*/site-packages"),
        os.path.expanduser("~/.venv/lib/python*/site-packages"),
        "/usr/local/lib/python*/site-packages",
        "/usr/lib/python3/dist-packages",
    ]
    pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for p in cands:
        for hit in glob.glob(p):
            if not os.path.isdir(os.path.join(hit, "astrbot")):
                continue
            # 版本不匹配的站点包（如 uv 装的 3.12 包 + 3.11 解释器）import 必炸，跳过
            if "python3." in hit and pyver not in hit:
                continue
            return hit
    return ""


def schema_defaults() -> dict:
    """把 _conf_schema.json 的 default 摊平成 AstrBot 运行时的配置树。

    这样测的是插件真实默认配置（含 msg_not_admin 等话术），不是测试自己编的期望值。
    """
    schema = json.load(open(os.path.join(BASE, "_conf_schema.json"), encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict) and node.get("type") == "object" and isinstance(node.get("items"), dict):
            return {k: walk(v) for k, v in node["items"].items()}
        if isinstance(node, dict) and "default" in node:
            return node["default"]
        return {}

    return {k: walk(v) for k, v in schema.items()}


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, m):      self.lines.append(f"[log] {m}")
    def warning(self, m):   self.lines.append(f"[warn] {m}")
    def error(self, m):     self.lines.append(f"[err] {m}")
    def exception(self, m): self.lines.append(f"[exc] {m}")
    def debug(self, m):     pass


class FakeEvent:
    """够用的 AstrMessageEvent 替身：只实现指令链路真正会碰到的字段。"""

    _seq = 0

    def __init__(self, text: str, *, group: str = "G1", sender: str = "U1",
                 name: str = "小明", role: str = "admin", msg_id: str = "",
                 mentioned: bool = False):
        FakeEvent._seq += 1
        self._text = text
        self._group = group
        self._sender = sender
        self._name = name
        self.role = role
        self.sent: list[str] = []
        self.call_llm = False
        mentions = []
        if mentioned:
            mentions.append(type("M", (), {"is_you": True, "member_openid": "BOT"})())
        raw = type("Raw", (), {"mentions": mentions, "author": None})()
        # 每条消息一个唯一 message_id：复用同一个 id 会命中 _run_command 的防重入去重
        self.message_obj = type("MO", (), {
            "message_id": msg_id or f"MSG{FakeEvent._seq}", "raw_message": raw})()

    def get_group_id(self):    return self._group
    def get_sender_id(self):   return self._sender
    def get_sender_name(self): return self._name
    def get_platform_id(self): return "pid1"

    async def send(self, chain):
        # MessageChain([Plain(text)]) -> 取纯文本，模拟真实回复内容
        parts = []
        for seg in getattr(chain, "chain", []) or []:
            parts.append(str(getattr(seg, "text", "") or ""))
        self.sent.append("".join(parts))


def build_plugin(main_lib, tmp: str, log: _Log):
    import types

    obj = types.SimpleNamespace(
        logger=log,
        mod=moderation.ModerationStore(tmp),
        config=schema_defaults(),
        _last_cmd_msg_id="",
        _last_cmd_ts=0.0,
        _qq_group_admins={},
    )
    obj.context = types.SimpleNamespace(get_config=lambda: {})
    # 指令链路上的真实方法
    for name in ("_run_command", "_handle_command", "_cmd_admin_register", "_is_admin",
                 "_read_config_value", "_read_path", "_render", "_reply", "_mod_tpl",
                 "_mod_group_allowed", "_mod_group_enabled", "_mod_settings",
                 "_get_qq_role",
                 "_is_qq_admin_openid", "_is_mentioned_bot", "_ensure_member_event_injected"):
        if hasattr(main_lib.QQGroupManager, name):
            obj.__dict__[name] = types.MethodType(getattr(main_lib.QQGroupManager, name), obj)
    # 与「管理登记」无关的副作用：桩掉
    obj._ensure_poll_task = lambda: None
    obj._record_recent_msg = lambda e: None

    async def _no_welcome(e):
        return None

    obj._maybe_welcome_new_group = _no_welcome
    obj._extract_text = lambda e: e._text
    return obj


def run(plugin, text: str, **kw) -> FakeEvent:
    ev = FakeEvent(text, **kw)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(plugin._run_command(ev))
    finally:
        loop.close()
    return ev


def main() -> int:
    site = find_astrbot_site()
    if site:
        sys.path.insert(0, site)
    try:
        main_lib = load("main")

    except Exception as e:                                          # noqa: BLE001
        skip(f"拿不到 astrbot 依赖（{e}），整层跳过")
        print("\n⏭ 全部跳过")
        return 0

    log = _Log()
    tmp = tempfile.mkdtemp(prefix="selftest_admin_")
    plugin = build_plugin(main_lib, tmp, log)
    G = "G1"
    not_admin_tpl = plugin.config["templates"]["msg_not_admin"]

    print("\n[1] 私聊里发指令")
    ev = run(plugin, "/管理登记", group="")
    ok(any("只能在群里" in s for s in ev.sent), f"明确拒绝而不是静默 -> {ev.sent}")

    print("\n[2] 群不在管理名单（basic.target_group_openids 不含本群）")
    plugin.config["basic"]["target_group_openids"] = ["OTHER_GROUP"]
    ev = run(plugin, "/管理登记", sender="U_ADMIN0")
    ok(ev.sent == [] and plugin.mod.get_admins(G) == [], "按配置静默忽略，不打扰")
    plugin.config["basic"]["target_group_openids"] = []

    print("\n[3] 非管理员尝试登记")
    ev = run(plugin, "/管理登记", sender="U_LOW", role="member")
    expect = not_admin_tpl.replace("{username}", "小明")
    ok(any(expect in s for s in ev.sent), f"提示无权限（插件默认话术）-> {ev.sent}")
    ok(plugin.mod.get_admins(G) == [], "非管理员不会写进名单")

    print("\n[4] 管理员登记自己")
    ev = run(plugin, "/管理登记", sender="U_ADMIN", name="大管家")
    admins = plugin.mod.get_admins(G)
    ok(len(admins) == 1 and admins[0]["openid"] == "U_ADMIN", f"名单 +1 -> {admins}")
    ok(any("登记成功" in s or "已加入" in s for s in ev.sent), f"回执话术 -> {ev.sent}")
    ok(admins[0]["role"] == "astrbot_admin", f"记下角色来源 -> {admins[0]['role']}")

    print("\n[5] 重复登记（幂等）")
    run(plugin, "/管理登记", sender="U_ADMIN", name="大管家")
    ok(len(plugin.mod.get_admins(G)) == 1, "同一个人不会重复占位")

    print("\n[6] /管理登记 列表")
    ev = run(plugin, "/管理登记 列表", sender="U_ADMIN")
    ok(any("大管家" in s for s in ev.sent), f"列出名单 -> {ev.sent}")

    print("\n[7] 不带斜杠「管理登记」")
    run(plugin, "管理登记", sender="U_ADMIN2", name="二管家", mentioned=True)
    ok(len(plugin.mod.get_admins(G)) == 2,
       f"@ 机器人时无斜杠写法生效 -> {[a['name'] for a in plugin.mod.get_admins(G)]}")
    ev = run(plugin, "管理登记", sender="U_ADMIN2", name="二管家", mentioned=False)
    ok(len(plugin.mod.get_admins(G)) == 2 and ev.sent == [],
       "没 @ 机器人时不误触发（普通聊天安全）")

    print("\n[8] 关掉「变体累计提醒」后登记")
    plugin.mod.set_settings(G, {"variant_strike_notify": False})
    ev = run(plugin, "/管理登记", sender="U_ADMIN3", name="三管家")
    ok(any("登记成功" in s or "已加入" in s for s in ev.sent), "登记本身仍然成功")
    ok(any("未开启" in s for s in ev.sent), f"额外提示暂时收不到 @ -> {ev.sent}")
    plugin.mod.set_settings(G, {"variant_strike_notify": True})

    print("\n[9] /管理登记 取消")
    ev = run(plugin, "/管理登记 取消", sender="U_ADMIN")
    left = [a["openid"] for a in plugin.mod.get_admins(G)]
    ok("U_ADMIN" not in left, f"已移出名单 -> {left}")
    ok(any(("取消" in s or "移出" in s or "移除" in s) for s in ev.sent), f"回执话术 -> {ev.sent}")

    print("\n[10] 没登记过的人取消")
    ev = run(plugin, "/管理登记 取消", sender="U_NOBODY")
    ok(any(("不在" in s or "没有" in s or "未" in s) for s in ev.sent),
       f"明确告知未登记 -> {ev.sent}")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：")
        for f in FAILED:
            print("   - " + f)
        return 1
    print(f"✅ 全部通过（跳过 {len(SKIPPED)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
