"""入群欢迎（GROUP_MEMBER_ADD）离线单元测试。

跑法（在插件目录下）：
    python3 -m unittest discover -s tests -v

完全离线：QQApi 换成记录器，只校验「什么时候该发、发出去的是什么、
多条路径会不会重复欢迎」。真实网关推不推事件得在沙箱群联调。
"""

import importlib
import sys
import time
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR.parent))
sys.path.insert(0, str(_PLUGIN_DIR))

_m = importlib.import_module(_PLUGIN_DIR.name + ".main")
QQGroupManager = _m.QQGroupManager
QQApiError = _m.QQApiError

GROUP = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
MEMBER = "C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6"
APPID = "1903123456"


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **kw: None


class _Members:
    """最小成员档案桩：只实现入群欢迎用到的 get / seen。"""

    def __init__(self):
        self.rows = {}

    def get(self, g, m):
        return self.rows.get((g, m))

    def seen(self, g, m, **kw):
        row = self.rows.setdefault((g, m), {"nickname": "", "first_seen": time.time()})
        return row


class _Mod:
    def get_group_meta(self, g):
        return {"name": "测试群"}


class _Api:
    def __init__(self, fail_markdown=False, fail_all=False):
        self.calls = []
        self.fail_markdown = fail_markdown
        self.fail_all = fail_all

    async def send_group_message(self, g, content, event_id="", msg_id="", markdown=False):
        if self.fail_all or (markdown and self.fail_markdown):
            raise QQApiError("发送失败", code=40034105)
        self.calls.append({"group": g, "content": content, "markdown": markdown})
        return {"ok": True}


def make_plugin(config=None, api=None, members_on=True, tpl_formats=None):
    class Stub(QQGroupManager):
        def _platform_appid(self, platform_id):
            return APPID

        def _members_on(self):
            return members_on

        def _api_for_group_or_default(self, group_openid, fallback_pid=None):
            return api

        def _group_in_scope(self, group_openid, platform_id=None):
            return True

    p = Stub.__new__(Stub)
    p.logger = _Logger()
    p.config = config if config is not None else {}
    p.members = _Members()
    p.mod = _Mod()
    p._join_welcomed = {}
    p._tpl_formats = tpl_formats or {}
    p._apis = {}
    return p


def cfg(welcome=True, tpl="🎉 欢迎 {at} 加入本群！"):
    return {
        "member_event": {"enable_member_add_welcome": welcome},
        "templates": {"msg_welcome_after_approve": tpl},
    }


def event(**kw):
    data = {"timestamp": 1784276757, "group_openid": GROUP, "member_openid": MEMBER}
    data.update(kw)
    return data


class JoinWelcomeTest(unittest.IsolatedAsyncioTestCase):
    async def test_sends_welcome_with_at_tag(self):
        api = _Api()
        p = make_plugin(cfg(), api)

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0]["group"], GROUP)
        self.assertEqual(
            api.calls[0]["content"],
            f'🎉 欢迎 <qqbot-at-user id="{MEMBER}" /> 加入本群！',
        )
        self.assertFalse(api.calls[0]["markdown"])

    async def test_disabled_config_sends_nothing(self):
        api = _Api()
        p = make_plugin(cfg(welcome=False), api)

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(api.calls, [])

    async def test_welcome_works_even_if_registry_off(self):
        """回归：旧实现里的「成员档案」开关曾把整个入群事件一起 return 掉。"""
        api = _Api()
        p = make_plugin(cfg(), api, members_on=False)

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(len(api.calls), 1)

    async def test_duplicate_event_only_welcomes_once(self):
        api = _Api()
        p = make_plugin(cfg(), api)

        await p._on_group_member_add(event(), "bot1")
        await p._on_group_member_add(event(), "bot1")  # 断线重连补事件

        self.assertEqual(len(api.calls), 1)

    async def test_approval_path_marks_dedupe(self):
        """审批通过刚欢迎过，紧接着的进群事件不该再发一条。"""
        api = _Api()
        p = make_plugin(cfg(), api)
        p._join_welcomed[f"{GROUP}:{MEMBER}"] = time.time()

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(api.calls, [])

    async def test_dedupe_window_expires(self):
        api = _Api()
        p = make_plugin(cfg(), api)
        p._join_welcomed[f"{GROUP}:{MEMBER}"] = time.time() - 601

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(len(api.calls), 1)

    async def test_markdown_format_used_when_template_is_markdown(self):
        api = _Api()
        p = make_plugin(
            cfg(tpl="# 🎉 欢迎 {at}\n\n本群用 /群管 帮助 看指令"),
            api,
            tpl_formats={"msg_welcome_after_approve": {"type": "markdown"}},
        )

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(len(api.calls), 1)
        self.assertTrue(api.calls[0]["markdown"])
        self.assertIn("# 🎉 欢迎", api.calls[0]["content"])

    async def test_markdown_falls_back_to_text(self):
        api = _Api(fail_markdown=True)
        p = make_plugin(
            cfg(), api,
            tpl_formats={"msg_welcome_after_approve": {"type": "markdown"}},
        )

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(len(api.calls), 1)
        self.assertFalse(api.calls[0]["markdown"])  # 退回纯文本，欢迎语不丢

    async def test_failed_send_keeps_no_dedupe(self):
        """发送失败不记去重键，下次事件还能重试。"""
        api = _Api(fail_all=True)
        p = make_plugin(cfg(), api)

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(api.calls, [])
        self.assertEqual(p._join_welcomed, {})

    async def test_username_from_registry(self):
        api = _Api()
        p = make_plugin(cfg(tpl="欢迎 {username} 加入本群"), api)
        p.members.seen(GROUP, MEMBER)["nickname"] = "小明"

        await p._on_group_member_add(event(), "bot1")

        self.assertEqual(api.calls[0]["content"], "欢迎 小明 加入本群")


if __name__ == "__main__":
    unittest.main()
