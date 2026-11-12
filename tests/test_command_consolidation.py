"""回归测试：指令整合（1.5.0）。

原则：**只合并入口，不废掉老写法**。
    /群管 禁言 解除 @x  ==  /群管 解禁 @x
    /群管 审批 通过 1    ==  /群管 通过 1
    /群管 状态 自检      ==  /群管 自检

跑法（插件目录下）：
    python3 -m unittest tests.test_command_consolidation -v
"""

import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

QQGroupManager = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.main"
).QQGroupManager


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        self.p = object.__new__(QQGroupManager)

    def test_legacy_names_fold_into_groups(self):
        cases = {
            "/解禁": ("/禁言", "解除"),
            "/查禁言": ("/禁言", "名单"),
            "/禁言历史": ("/禁言", "历史"),
            "/通过": ("/审批", "通过"),
            "/拒绝": ("/审批", "拒绝"),
            "/汇报": ("/状态", "日报"),
            "/体检": ("/状态", "群"),
            "/自检": ("/状态", "自检"),
            "/群名": ("/状态", "群名"),
        }
        for old, (new_cmd, sub) in cases.items():
            cmd, parts = self.p._normalize_aliases(old, [old, "@某人", "5"])
            self.assertEqual(cmd, new_cmd, old)
            self.assertEqual(parts[:2], [new_cmd, sub], old)
            self.assertEqual(parts[2:], ["@某人", "5"], old)   # 参数一个不丢

    def test_canonical_names_untouched(self):
        cmd, parts = self.p._normalize_aliases("/禁言", ["/禁言", "@/x", "10分钟"])
        self.assertEqual((cmd, parts), ("/禁言", ["/禁言", "@/x", "10分钟"]))

    def test_alias_detected_from_message(self):
        p = self.p
        p._extract_text = lambda e: "/群管 解禁 @某人"
        self.assertEqual(p._cmd_alias_of(SimpleNamespace(), "/禁言"), "/解禁")
        p._extract_text = lambda e: "解禁 @某人"          # 不带 / 也认
        self.assertEqual(p._cmd_alias_of(SimpleNamespace(), "/禁言"), "/解禁")
        p._extract_text = lambda e: "/群管 禁言 @某人 10分钟"
        self.assertEqual(p._cmd_alias_of(SimpleNamespace(), "/禁言"), "/禁言")


class StatusRoutingTest(unittest.TestCase):
    def setUp(self):
        self.p = object.__new__(QQGroupManager)
        self.calls = []
        for name in ("_cmd_report", "_cmd_selfcheck"):
            setattr(self.p, name, (lambda n: lambda event: self._rec(n))(name[1:]))
        self.p._cmd_presence = lambda event, parts: self._rec("presence", parts)
        self.p._cmd_group_name = lambda event: self._rec("group_name")
        self.p._reply = lambda event, text: self._rec("reply", text)

    def _rec(self, name, arg=None):
        self.calls.append((name, arg))

        async def _done():
            return None

        return _done()

    def _run(self, parts):
        self.calls = []
        asyncio.run(self.p._cmd_status(SimpleNamespace(), parts))
        return self.calls

    def test_default_is_daily_report(self):
        self.assertIn("cmd_report", [c[0] for c in self._run(["/状态"])])

    def test_subcommands_route(self):
        self.assertIn("cmd_selfcheck", [c[0] for c in self._run(["/状态", "自检"])])
        self.assertEqual(self._run(["/状态", "群", "清理"]),
                         [("presence", ["/体检", "清理"])])
        self.assertEqual(self._run(["/状态", "群名"]), [("group_name", None)])

    def test_unknown_sub_shows_usage(self):
        calls = self._run(["/状态", "乱写"])
        self.assertEqual(calls[0][0], "reply")
        self.assertIn("用法：/群管 状态", calls[0][1])


if __name__ == "__main__":
    unittest.main()
