"""回归测试：词库上传不能借 target_id 把文件写到数据目录之外。

历史 bug：`upload(mode="overwrite", target_id=任意字符串)` 在 target_id
查不到词库时，仍然把 target_id 当成 id 用；`_write_custom()` 再从
"custom-" 后面截出文件名，于是 `custom-../../x` 会写到数据目录外面。
现在只认已存在的词库 id，并且文件名再做一次 slug 兜底。
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent.parent))

lexicon = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.lexicon")


class _Logger:
    def __getattr__(self, name):
        return lambda *a, **k: None


class LexiconUploadPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.store = lexicon.LexiconManager(
            data_dir=str(self.data), plugin_dir=str(PLUGIN_DIR), logger=_Logger()
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_traversal_target_id_is_ignored(self):
        r = self.store.upload(
            name="安全词库",
            content="广告\n刷屏",
            mode="overwrite",
            target_id="custom-../../escape",
        )
        self.assertTrue(r.get("ok"), r)
        escaped = self.data.parent / "escape.json"
        self.assertFalse(escaped.exists(), "文件被写到数据目录之外了")
        # 落盘文件必须仍在词库目录里
        self.assertEqual(Path(r["id"]).parent.name if "/" in r["id"] else ".", ".")
        files = list((self.data / lexicon.BUILTIN_DIRNAME).glob("*.json"))
        self.assertTrue(files)
        for f in files:
            self.assertEqual(f.resolve().parent,
                             (self.data / lexicon.BUILTIN_DIRNAME).resolve())

    def test_overwrite_existing_keeps_same_file(self):
        first = self.store.upload(name="测试库", content="广告")
        self.assertTrue(first["ok"])
        second = self.store.upload(
            name="换个名字", content="刷屏", mode="overwrite", target_id=first["id"]
        )
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["id"], first["id"])

    def test_overwrite_unknown_target_falls_back_to_name(self):
        r = self.store.upload(
            name="新库", content="广告", mode="overwrite", target_id="custom-不存在"
        )
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["id"], "custom-新库")


if __name__ == "__main__":
    unittest.main()
