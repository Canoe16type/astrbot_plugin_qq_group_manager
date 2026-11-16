"""判定记录纯函数测试：摘要、原文归档、导出筛选。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.qgm.features.audit import record  # noqa: E402


class RecordTest(unittest.TestCase):
    def test_verdict_text_truncates_and_counts(self):
        d = record.verdict_text({"text": "字" * 1200, "images": [1, 2]}, cap=1000)
        self.assertEqual(len(d["text"]), 1000)
        self.assertTrue(d["text_truncated"])
        self.assertEqual(d["text_len"], 1200)
        self.assertEqual(d["images"], 2)

    def test_fmt_line_mute_and_revoked(self):
        v = {"id": 7, "created_at": 0, "target_name": "小明", "violation": True,
             "category": "ads", "action": "mute", "ban_seconds": 3600, "confidence": 0.87}
        line = record.fmt_verdict_line(v, {"ads": "广告"}, lambda s: "1小时")
        self.assertIn("#7", line)
        self.assertIn("小明", line)
        self.assertIn("广告", line)
        self.assertIn("禁言 1小时", line)
        self.assertIn("0.87", line)
        v["revoked"] = True
        self.assertIn("已撤销", record.fmt_verdict_line(v, {"ads": "广告"}, lambda s: "1小时"))

    def test_filter_records_group_since_order(self):
        items = [
            {"id": 3, "group": "g1", "created_at": 300},
            {"id": 2, "group": "g2", "created_at": 200},
            {"id": 1, "group": "g1", "created_at": 100},
        ]
        out = record.filter_records(items, group="g1", since=150, limit=10)
        self.assertEqual([x["id"] for x in out], [3])
        out = record.filter_records(items, group="g1", since=0, limit=10)
        self.assertEqual([x["id"] for x in out], [1, 3], "输出应为旧→新")
        out = record.filter_records(items, group="g1", since=0, limit=1)
        self.assertEqual([x["id"] for x in out], [3], "limit 按新→旧截断后再反转")


if __name__ == "__main__":
    unittest.main()
