"""踢人指令与记录测试。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.qgm.features.kick import commands, record  # noqa: E402
from plugins.astrbot_plugin_qq_group_manager.qgm.integrations.qq_api import QQApiError  # noqa: E402


class _User:
    def __init__(self, oid, nick="", bot=False):
        self.member_openid = oid
        self.member_nickname = nick
        self.username = ""
        self.bot = bot


class _Obj:
    def __init__(self):
        self.message_obj = type("M", (), {"raw_message": type("R", (), {"mentions": [
            _User("u1", "小明"), _User("u2", "小华"), _User("bot1", "机器人", bot=True)]})()})
        self._platform = "qq_official_1"
        self._group = "g1"
        self.replies = []

    def get_group_id(self): return self._group
    def get_sender_id(self): return "admin1"
    def get_sender_name(self): return "管理员"
    def get_platform_id(self): return self._platform


class _Api:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    async def batch_remove_members(self, group, ids):
        self.calls.append((group, ids))
        if self.fail:
            raise self.fail
        return {"remove_members_result": "success", "add_to_member_blacklist_fail_openids": []}


class _Host:
    def __init__(self, api):
        self.api = api
        self.tmp = tempfile.TemporaryDirectory()
        self.replies = []

    def _plugin_data_dir(self): return self.tmp.name
    def _get_api(self, pid): return self.api
    def _ctx(self, *a, **k): return ""
    def _render(self, key, **kw):
        tpl = {
            "msg_kick_usage": "用法：/群管 踢人 @成员… [理由]",
            "msg_kick_success": "已移除 {names}（记录 #{id}）",
            "msg_kick_no_permission": "踢人接口未开通（11253）",
            "msg_kick_perm_owner": "群主关闭了操作权限（40103012）",
            "msg_kick_perm_bot": "机器人侧关闭了操作权限（40103011）",
            "msg_kick_history_empty": "还没有踢人记录",
            "msg_kick_history_title": "踢人记录（最近 {count} 条）：{lines}",
            "msg_kick_too_many": "最多 {max} 人",
            "msg_kick_fail": "踢人失败：{error}",
        }[key]
        return tpl.format(**kw)
    logger = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None,
                             "error": lambda *a, **k: None})()

    async def _reply(self, event, text): self.replies.append(text)


class KickTest(unittest.TestCase):
    def test_success_removes_bots_and_records(self):
        api = _Api(); host = _Host(api); ev = _Obj()
        asyncio.run(commands.cmd_kick(host, ev, ["/踢人", "刷屏"]))
        self.assertEqual(api.calls[0][1], ["u1", "u2"], "不应包含机器人")
        self.assertTrue(any("已移除" in x for x in host.replies))
        rows = record.recent(host, "g1")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["reason"], "刷屏")
        host.tmp.cleanup()

    def test_11253_gives_actionable_message_and_failure_record(self):
        api = _Api(QQApiError("no permission", code=11253)); host = _Host(api); ev = _Obj()
        asyncio.run(commands.cmd_kick(host, ev, ["/踢人", "@x"]))
        self.assertTrue(any("11253" in x for x in host.replies))
        row = record.recent(host, "g1")[0]
        self.assertFalse(row["ok"])
        host.tmp.cleanup()

    def test_no_mention_shows_usage(self):
        host = _Host(_Api()); ev = _Obj()
        ev.message_obj.raw_message.mentions = []
        asyncio.run(commands.cmd_kick(host, ev, ["/踢人"]))
        self.assertTrue(any("用法" in x for x in host.replies))
        host.tmp.cleanup()

    def test_history_lists_records(self):
        host = _Host(_Api())
        record.add(host, {"group": "g1", "targets": [{"openid": "u1", "name": "小明"}],
                          "ok": True, "ts": 1})
        ev = _Obj()
        asyncio.run(commands.cmd_kick(host, ev, ["/踢人", "记录"]))
        self.assertTrue(any("小明" in x for x in host.replies))
        host.tmp.cleanup()

    def test_permission_error_codes_have_specific_hints(self):
        for code, needle in ((40103012, "群主关闭了操作权限"), (40103011, "机器人侧关闭了操作权限")):
            host = _Host(_Api(QQApiError("denied", code=code))); ev = _Obj()
            asyncio.run(commands.cmd_kick(host, ev, ["/踢人", "@x"]))
            self.assertTrue(any(needle in x for x in host.replies), (code, host.replies))
            host.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
