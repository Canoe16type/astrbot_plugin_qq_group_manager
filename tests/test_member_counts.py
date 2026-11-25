"""成员终身计数与按群日退群序号。"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.member_registry import MemberRegistry  # noqa: E402


class MemberCountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = MemberRegistry(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_join_leave_rejoin_and_auto_delete_keeps_counts(self):
        self.m.seen("g", "u1", ts=1000, count_msg=False)
        self.assertEqual(self.m.get_counts("g", "u1")["join_count"], 1)
        self.m.mark_left("g", "u1", ts=1100)
        c = self.m.get_counts("g", "u1")
        self.assertEqual(c["leave_count"], 1)
        self.m.seen("g", "u1", ts=1200, count_msg=False)
        c = self.m.get_counts("g", "u1")
        self.assertEqual(c["join_count"], 2)
        self.assertEqual(c["leave_count"], 1)
        self.m.mark_left("g", "u1", ts=1300)
        self.m.delete("g", "u1")
        c = self.m.get_counts("g", "u1")
        self.assertEqual(c["join_count"], 2)
        self.assertEqual(c["leave_count"], 2)

    def test_daily_counter_natural_day(self):
        day = time.mktime(time.strptime("2026-11-25 12:00", "%Y-%m-%d %H:%M"))
        nxt = day + 86400
        self.assertEqual(self.m.bump_leave_today("g", day), 1)
        self.assertEqual(self.m.bump_leave_today("g", day + 60), 2)
        self.assertEqual(self.m.bump_leave_today("g", nxt), 1)


if __name__ == "__main__":
    unittest.main()
