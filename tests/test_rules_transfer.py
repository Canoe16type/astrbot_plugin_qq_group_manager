"""回归测试：群规导出 / 转移到其他群的存储层行为。

导出是纯前端格式化，这里只锁存储层：
- 追加模式必须给复制过去的规则重新分配 id（否则按 id 删除会误伤）；
- 覆盖模式要先清空目标群；
- 源群规则不能被改动；
- 源群没有规则时明确报错，而不是静默成功。

跑法（插件目录下）：
    python3 -m unittest tests.test_rules_transfer -v
"""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

moderation = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.moderation")


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _rule(rid, name, secs=600):
    return {"id": rid, "name": name, "desc": name + "说明", "category": "ad",
            "severity": "medium", "seconds": secs, "enabled": True, "keywords": []}


class TransferRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = moderation.ModerationStore(Path(self.tmp.name), _Logger())

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_keeps_target_and_dedupes_ids(self):
        self.store.set_rules("g-src", [_rule("rule_01", "广告"), _rule("rule_02", "刷屏")])
        self.store.set_rules("g-dst", [_rule("rule_01", "目标群原有")])

        res = self.store.transfer_rules("g-src", "g-dst", "append")
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["total"], 3)

        dst = self.store.get_rules("g-dst")
        self.assertEqual(len(dst), 3)
        ids = [r["id"] for r in dst]
        self.assertEqual(len(ids), len(set(ids)), "目标群内 id 不允许重复")
        self.assertEqual(dst[0]["name"], "目标群原有")
        self.assertIn("广告", [r["name"] for r in dst])

    def test_replace_clears_target(self):
        self.store.set_rules("g-src", [_rule("rule_01", "广告")])
        self.store.set_rules("g-dst", [_rule("rule_01", "旧规则一"), _rule("rule_02", "旧规则二")])

        res = self.store.transfer_rules("g-src", "g-dst", "replace")
        self.assertTrue(res["ok"])
        self.assertEqual(res["total"], 1)
        dst = self.store.get_rules("g-dst")
        self.assertEqual([r["name"] for r in dst], ["广告"])

    def test_source_untouched(self):
        self.store.set_rules("g-src", [_rule("rule_01", "广告")])
        self.store.set_rules("g-dst", [])
        self.store.transfer_rules("g-src", "g-dst", "append")
        self.assertEqual([r["name"] for r in self.store.get_rules("g-src")], ["广告"])

    def test_only_selected_ids_are_transferred(self):
        self.store.set_rules("g-src", [_rule("rule_01", "广告"), _rule("rule_02", "刷屏"),
                                       _rule("rule_03", "引战")])
        self.store.set_rules("g-dst", [])
        res = self.store.transfer_rules("g-src", "g-dst", "append", ids=["rule_02"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 1)
        self.assertEqual([r["name"] for r in self.store.get_rules("g-dst")], ["刷屏"])

    def test_stale_ids_report_error(self):
        """勾选的规则在源群已被删：明确报错，别静默成功。"""
        self.store.set_rules("g-src", [_rule("rule_01", "广告")])
        self.store.set_rules("g-dst", [])
        res = self.store.transfer_rules("g-src", "g-dst", "append", ids=["rule_99"])
        self.assertFalse(res["ok"])
        self.assertIn("已不存在", res["error"])
        self.assertEqual(self.store.get_rules("g-dst"), [])

    def test_empty_source_reports_error(self):
        self.store.set_rules("g-src", [])
        res = self.store.transfer_rules("g-src", "g-dst", "append")
        self.assertFalse(res["ok"])
        self.assertEqual(res["count"], 0)
        self.assertIn("没有群规", res["error"])
        self.assertEqual(self.store.get_rules("g-dst"), [])


if __name__ == "__main__":
    unittest.main()
