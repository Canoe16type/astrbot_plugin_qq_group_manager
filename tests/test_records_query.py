"""统一记录查询层。"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

import importlib  # noqa: E402
query = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.qgm.features.records.query")  # noqa: E402


def _host():
    return SimpleNamespace(
        mod=SimpleNamespace(list_verdicts=lambda group="", limit=100: [
            {"id": 1, "group": "g", "target": "u1", "target_name": "小明",
             "created_at": 100, "executed": True, "action": "mute",
             "reason": "广告", "category_public": "广告引流"},
            {"id": 2, "group": "g", "target": "u2", "target_name": "小红",
             "created_at": 300, "executed": False, "dry_run": True,
             "action": "none", "reason": "测试"},
        ]),
        _mute_history={"g": [
            {"id": 1, "group_openid": "g", "member_openid": "u1", "username": "小明",
             "mute_at": 200, "status": "active", "reason": "刷屏", "seconds": 600},
        ]},
        _recall_history={"g": [
            {"id": 1, "group_openid": "g", "recalled_at": 150, "operator": "op",
             "message_id": "m1", "event_id": "e1", "source": "quote"},
        ]},
    )


class RecordsQueryTest(unittest.TestCase):
    def setUp(self):
        self.host = _host()
        self.kick = [{"id": 7, "ts": 250, "group": "g", "ok": True,
                      "operator": "op", "reason": "刷屏",
                      "targets": [{"openid": "u1", "name": "小明"},
                                  {"openid": "u3", "name": "小刚"}]}]
        self.patcher = patch.object(query.kick_record, "iter_rows", return_value=self.kick)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_order_and_prefix(self):
        rows = query.query(self.host, group="g", limit=10)
        self.assertEqual([r.display_id for r in rows], ["V2", "K7", "M1", "R1", "V1"])
        self.assertFalse(rows[0].ok)

    def test_target_matches_kick_multi(self):
        rows = query.query(self.host, group="g", target_id="u3")
        self.assertEqual([r.display_id for r in rows], ["K7"])
        rows = query.query(self.host, group="g", target_id="u1")
        self.assertEqual({r.display_id for r in rows}, {"K7", "M1", "V1"})

    def test_kind_and_since(self):
        self.assertEqual([r.kind for r in query.query(self.host, group="g", kinds=["mute"])], ["mute"])
        rows = query.query(self.host, group="g", since=180)
        self.assertEqual([r.display_id for r in rows], ["V2", "K7", "M1"])

    def test_legacy_recall(self):
        row = query.get(self.host, "R1", group="g")
        self.assertTrue(row.legacy)
        self.assertNotIn("R1", [r.display_id for r in query.query(self.host, group="g", target_id="u1")])

    def test_get_invalid(self):
        self.assertIsNone(query.get(self.host, "X1", group="g"))
        self.assertIsNone(query.get(self.host, "V999", group="g"))


if __name__ == "__main__":
    unittest.main()
