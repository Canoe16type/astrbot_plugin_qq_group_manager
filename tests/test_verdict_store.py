"""P1 回归测试：判定记录的磁盘存储（JSONL）。

要求：
- 记录只写盘，不常驻内存；
- 旧 mod_verdicts.json 能自动迁移，迁移后旧文件改名留档；
- 条数上限可配置（只在追加时裁最旧的）；
- 「删除 N 天以前」只能手动触发，不能自动定期删。
"""

import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

moderation = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.moderation")


class _Logger:
    def _noop(self, *a, **k):
        pass

    debug = info = warning = error = _noop


class VerdictStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = moderation.ModerationStore(str(self.dir), _Logger())

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, text, ts=None):
        return self.store.add_verdict({
            "group": "g1", "text": text, "violation": True, "executed": True,
            "action": "mute", "ban_seconds": 60,
            "created_at": ts or int(time.time()),
        })

    def test_appends_to_disk_not_memory(self):
        self._add("a")
        self._add("b")
        log = self.dir / "mod_verdicts.jsonl"
        self.assertTrue(log.exists())
        lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.store.count_verdicts(), 2)
        self.assertEqual(self.store.count_verdicts("g1"), 2)

    def test_newest_first_and_limit(self):
        for i in range(5):
            self._add(f"t{i}", ts=int(time.time()) - i)
        # 列表按「写入顺序倒序」返回（最新追加的在最前），不依赖 created_at
        items = self.store.list_verdicts("g1", limit=3)
        self.assertEqual([x["text"] for x in items], ["t4", "t3", "t2"])

    def test_max_records_keeps_newest(self):
        self.store.verdict_max_records = 3
        for i in range(5):
            self._add(f"t{i}")
        texts = [v["text"] for v in self.store.list_verdicts("g1", limit=99)]
        self.assertEqual(texts, ["t4", "t3", "t2"])

    def test_prune_only_manual(self):
        old = int(time.time()) - 40 * 86400
        self._add("old1", ts=old)
        self._add("new1")
        self.assertEqual(self.store.count_verdicts(), 2)
        # 没有任何"自动清理"路径：不调用 prune 就不会少
        self.assertEqual(self.store.count_verdicts(), 2)
        removed = self.store.prune_older_than(30)
        self.assertEqual(removed, 1)
        self.assertEqual([v["text"] for v in self.store.list_verdicts("g1", limit=9)], ["new1"])

    def test_migrates_legacy_json(self):
        # 新目录里先放旧格式，再初始化 store
        legacy_dir = tempfile.TemporaryDirectory()
        self.addCleanup(legacy_dir.cleanup)
        p = Path(legacy_dir.name)
        (p / "mod_verdicts.json").write_text(json.dumps([
            {"id": 1, "group": "g1", "text": "旧记录", "violation": True,
             "executed": True, "created_at": int(time.time())},
        ], ensure_ascii=False), encoding="utf-8")

        store = moderation.ModerationStore(str(p), _Logger())
        self.assertEqual(store.count_verdicts(), 1)
        self.assertEqual(store.list_verdicts("g1", 10)[0]["text"], "旧记录")
        self.assertTrue((p / "mod_verdicts.jsonl").exists())
        self.assertTrue((p / "mod_verdicts.json.migrated").exists())

    def test_delete_and_update_persist(self):
        rec = self._add("x")
        self.assertTrue(self.store.update_verdict(rec["id"], feedback="false_positive"))
        reread = moderation.ModerationStore(str(self.dir), _Logger())
        self.assertEqual(reread.get_verdict(rec["id"])["feedback"], "false_positive")
        self.assertTrue(reread.delete_verdict(rec["id"]))
        self.assertIsNone(moderation.ModerationStore(str(self.dir), _Logger()).get_verdict(rec["id"]))


    def test_setter_updates_seq_for_next_append(self):
        """整表赋值后，meta 里的 seq 必须同步，否则重启后会复用 id。"""
        self.store.verdicts = [
            {"id": 5, "group": "g1", "created_at": int(time.time())},
            {"id": 7, "group": "g1", "created_at": int(time.time())},
        ]
        rec = self.store.add_verdict({"group": "g1", "text": "new"})
        self.assertEqual(rec["id"], 8)
        reread = moderation.ModerationStore(str(self.dir), _Logger())
        self.assertEqual(reread.add_verdict({"group": "g1", "text": "after restart"})["id"], 9)

    def test_migration_does_not_touch_json_when_jsonl_has_data(self):
        """JSONL 已有数据时，不能把用户恢复的旧 json 静默改名（那等于吞备份）。"""
        log = self.dir / "mod_verdicts.jsonl"
        log.write_text(json.dumps({"id": 1, "group": "g1", "text": "现有"}) + "\n", encoding="utf-8")
        legacy = self.dir / "mod_verdicts.json"
        legacy.write_text(json.dumps([{"id": 9, "group": "g1", "text": "备份"}]), encoding="utf-8")
        moderation.ModerationStore(str(self.dir), _Logger())
        self.assertTrue(legacy.exists(), "旧文件应保持原样")
        self.assertFalse((self.dir / "mod_verdicts.json.migrated").exists())


if __name__ == "__main__":
    unittest.main()
