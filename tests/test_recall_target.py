"""撤回记录的目标身份 / 事件 ID。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.main import QQGroupManager  # noqa: E402
from plugins.astrbot_plugin_qq_group_manager.qgm.features.members.cache import (  # noqa: E402
    _lookup_recent_msg,
)


class RecallTargetTest(unittest.TestCase):
    def test_lookup_by_message_id(self):
        host = object.__new__(QQGroupManager)
        host._recent_group_msgs = {
            "g1": [
                {"id": "m1", "sender": "u1", "sender_name": "小明"},
                {"id": "m2", "sender": "u2", "sender_name": "小红"},
            ]
        }
        self.assertEqual(_lookup_recent_msg(host, "g1", "m2")["sender"], "u2")
        self.assertEqual(_lookup_recent_msg(host, "g1", "missing"), {})

    def test_record_keeps_event_and_target(self):
        host = object.__new__(QQGroupManager)
        host._recall_seq = 0
        host._recall_history = {}
        host._read_config_value = lambda *a, **k: 0
        host._save_recall_history = lambda *a, **k: None
        rec = host._add_recall_record(
            group_openid="g1", platform_id="qq", message_id="m2",
            content="hi", source="quote", operator="op", operator_name="管理员",
            event_id="evt-1", target_openid="u2", target_name="小红",
        )
        self.assertEqual(rec["event_id"], "evt-1")
        self.assertEqual(rec["target_openid"], "u2")
        self.assertEqual(rec["target_name"], "小红")


if __name__ == "__main__":
    unittest.main()
