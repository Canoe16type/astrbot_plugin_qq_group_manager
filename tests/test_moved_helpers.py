"""搬迁后的共享 helper 冒烟：防止再次出现「模块搬了、导入没跟上」的 NameError。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager import main as main_mod  # noqa: E402


class MovedHelperSmokeTest(unittest.TestCase):
    def test_timezone_helpers(self):
        self.assertTrue(main_mod.active_timezone_name())
        self.assertIsNotNone(main_mod._active_tz())

    def test_mute_window_eval_does_not_raise(self):
        # 这条路径以前直接调 _active_tz()，搬走定义后会 NameError
        self.assertTrue(main_mod._mute_rule_live("always", []))
        self.assertFalse(main_mod._mute_rule_live("schedule", []))

    def test_time_formatters(self):
        self.assertTrue(main_mod.fmt_duration(3600))
        self.assertTrue(main_mod.fmt_ts(0))
        self.assertTrue(main_mod.fmt_rfc3339("2026-11-16T00:00:00Z"))

    def test_parse_duration(self):
        self.assertEqual(main_mod.parse_duration("5分钟"), 300)
        self.assertEqual(main_mod.parse_duration("一小时"), 3600)


if __name__ == "__main__":
    unittest.main()
