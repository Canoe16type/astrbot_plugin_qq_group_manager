"""`/群管 记录` 权限与输出。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.qgm.features.records import commands  # noqa: E402


class _Event:
    def __init__(self, sender="u1", group="g"):
        self.sender = sender
        self.group = group
        self.sent = []
    def get_group_id(self): return self.group
    def get_sender_id(self): return self.sender
    def get_platform_id(self): return "qq"
    def get_sender_name(self): return "小明"

    async def send(self, chain):
        self.sent.append(str(chain))


def _host(admin=False):
    h = SimpleNamespace()
    h._is_admin = lambda event: admin
    h._mentioned_openids = lambda event: []
    h._reply = _capture
    h.mod = SimpleNamespace(list_verdicts=lambda group="", limit=100: [
        {"id": 1, "group": "g", "target": "u1", "target_name": "小明",
         "created_at": 100, "executed": True, "action": "mute", "reason": "广告"},
        {"id": 2, "group": "g", "target": "u2", "target_name": "小红",
         "created_at": 200, "executed": True, "action": "mute", "reason": "刷屏"},
    ])
    h._mute_history = {"g": []}
    h._recall_history = {"g": []}
    h._plugin_data_dir = lambda: "/tmp/nonexistent-qgm"
    return h


async def _capture(event, text):
    event.sent.append(("reply", str(text)))


class RecordsCommandTest(unittest.TestCase):
    def test_router_handler_exists(self):
        from plugins.astrbot_plugin_qq_group_manager.main import QQGroupManager
        self.assertTrue(hasattr(QQGroupManager, "_cmd_records"))

    def _run(self, host, event, parts):
        async def fake_send(h, e, text):
            e.sent.append(("md", text))
        with patch.object(commands, "_send_markdown", fake_send):
            asyncio.run(commands.cmd_records(host, event, parts))
        return event.sent

    def test_member_requires_mine(self):
        sent = self._run(_host(False), _Event(), ["/记录"])
        self.assertEqual(sent[0][0], "reply")
        self.assertIn("我的", sent[0][1])

    def test_member_mine_only_self(self):
        sent = self._run(_host(False), _Event("u1"), ["/记录", "我的"])
        text = sent[-1][1]
        self.assertIn("V1", text)
        self.assertNotIn("V2", text)
        self.assertIn("仅记录本插件", text)

    def test_admin_all(self):
        sent = self._run(_host(True), _Event("admin"), ["/记录"])
        text = sent[-1][1]
        self.assertIn("V1", text)
        self.assertIn("V2", text)

    def test_member_cannot_read_other_detail(self):
        sent = self._run(_host(False), _Event("u1"), ["/记录", "详情", "V2"])
        self.assertIn("只能查看自己", sent[-1][1])

    def test_member_own_detail(self):
        sent = self._run(_host(False), _Event("u1"), ["/记录", "详情", "V1"])
        self.assertIn("记录 `V1`", sent[-1][1])

    def test_summary_admin_only(self):
        sent = self._run(_host(False), _Event("u1"), ["/记录", "汇总"])
        self.assertIn("管理员", sent[-1][1])


if __name__ == "__main__":
    unittest.main()
