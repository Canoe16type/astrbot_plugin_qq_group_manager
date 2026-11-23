"""纯文本模式下的 @ 显示。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.main import QQGroupManager  # noqa: E402


class _Members:
    def get(self, group, openid):
        return {"group_nickname": "小明"} if openid == "u1" else None


class _Event:
    def get_group_id(self): return "g1"
    def get_sender_id(self): return "u2"
    def get_sender_name(self): return "小红"


class PlainAtTest(unittest.TestCase):
    def setUp(self):
        self.p = object.__new__(QQGroupManager)
        self.p.members = _Members()
        self.ev = _Event()

    def test_member_nickname_wins(self):
        self.assertEqual(self.p._plain_at_name(self.ev, "u1", "g1"), "@小明")

    def test_sender_fallback(self):
        self.assertEqual(self.p._plain_at_name(self.ev, "u2", "g1"), "@小红")

    def test_unknown_falls_back_to_tail(self):
        self.assertEqual(self.p._plain_at_name(self.ev, "961D3A4628D52BC4", "g1"), "@D52BC4")


if __name__ == "__main__":
    unittest.main()
