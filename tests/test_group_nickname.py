"""群昵称（member_nickname）写入成员档案。"""

import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager import member_registry as mr  # noqa: E402


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


class GroupNicknameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = mr.MemberRegistry(self.tmp.name, _Log())

    def tearDown(self):
        self.tmp.cleanup()

    def test_group_nickname_becomes_display_name(self):
        self.reg.seen("g", "u1", name="QQ昵称", group_nickname="群昵称")
        rec = self.reg.get("g", "u1")
        self.assertEqual(rec["group_nickname"], "群昵称")
        self.assertEqual(rec["nickname"], "群昵称", "群昵称应优先展示")

    def test_manual_name_is_not_overwritten(self):
        self.reg.seen("g", "u1", name="QQ昵称", group_nickname="群昵称")
        self.reg.set_name("g", "u1", "人工备注名")
        self.reg.seen("g", "u1", name="QQ昵称", group_nickname="群昵称2")
        rec = self.reg.get("g", "u1")
        self.assertEqual(rec["nickname"], "人工备注名")
        self.assertEqual(rec["group_nickname"], "群昵称2")


if __name__ == "__main__":
    unittest.main()
