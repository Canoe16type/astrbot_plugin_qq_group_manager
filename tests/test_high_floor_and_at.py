"""高危地板与撤销 @ 标签回归。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager import moderation  # noqa: E402
from plugins.astrbot_plugin_qq_group_manager.main import QQGroupManager  # noqa: E402


class HighSeverityFloorTest(unittest.TestCase):
    def _rules(self):
        return [{"id": "rule_01", "name": "引人不适", "category": "other",
                 "severity": "low", "seconds": 600, "enabled": True}]

    def test_high_does_not_attach_to_low_rule_when_smart_off(self):
        out = moderation.decide_action(
            {"violation": True, "severity": "high", "category": "other", "confidence": 0.95},
            rules=self._rules(), smart=False, max_seconds=604800, rule_floor_high=True)
        self.assertEqual(out["action"], "mute")
        self.assertGreaterEqual(out["seconds"], 86400, "高危不应被低档规则压成 10 分钟")
        self.assertNotEqual(out["rule_id"], "rule_01", "高危不应误标成低档规则")

    def test_high_floor_still_works_with_smart(self):
        out = moderation.decide_action(
            {"violation": True, "severity": "high", "category": "other", "confidence": 0.95},
            rules=self._rules(), smart=True, max_seconds=604800, rule_floor_high=True)
        self.assertGreaterEqual(out["seconds"], 86400)


class RevokeAtTagTest(unittest.TestCase):
    def setUp(self):
        self.p = object.__new__(QQGroupManager)
        self.p._read_config_value = lambda *a, **k: ""
        self.p._tpl_formats = {}

    def _render(self, at):
        return self.p._render(
            "msg_mod_revoke", vid=26, at=at, punish="禁言 10分钟",
            unmute="已解除", operator="管理员", reason_line="")

    def test_openid_is_wrapped_once(self):
        out = self._render("961D3A4628D52BC4425AB4FA6B09EE9D")
        self.assertEqual(out.count("<qqbot-at-user"), 1)
        self.assertNotIn('<qqbot-at-user id="<qqbot-at-user', out)

    def test_already_tagged_is_not_wrapped_again(self):
        out = self._render('<qqbot-at-user id="961D3A4628D52BC4425AB4FA6B09EE9D" />')
        self.assertEqual(out.count("<qqbot-at-user"), 1)
        self.assertNotIn('<qqbot-at-user id="<qqbot-at-user', out)


if __name__ == "__main__":
    unittest.main()
