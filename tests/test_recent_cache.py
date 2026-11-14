"""回归测试：最近消息缓存落盘。

背景：插件重载会清空内存里的消息缓存，导致「重载前发的消息」被举报时定位不到发送者，
只能判定不能处罚（线上表现：提示"缓存过期/无法定位"）。现在缓存落盘，启动时恢复 10 分钟内的消息。
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

QQGroupManager = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.main"
).QQGroupManager


class _Logger:
    def _noop(self, *a, **k):
        pass

    debug = info = warning = error = _noop


def _plugin(tmpdir):
    p = object.__new__(QQGroupManager)
    p.logger = _Logger()
    p._recent_group_msgs = {}
    p._recent_cache_path = str(Path(tmpdir) / "recent_msgs.json")
    p._recent_saved_at = 0.0
    return p


class RecentCachePersistTest(unittest.TestCase):
    def test_roundtrip_survives_reload(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            p1 = _plugin(d)
            p1._recent_group_msgs = {"g1": [
                {"id": "m1", "text": "广告内容", "ts": now, "sender": "u1",
                 "sender_name": "小明", "images": []},
            ]}
            p1._save_recent_msgs(force=True)
            self.assertTrue((Path(d) / "recent_msgs.json").exists())

            p2 = _plugin(d)          # 模拟重载：新实例、空内存
            p2._load_recent_msgs()
            self.assertEqual(len(p2._recent_group_msgs["g1"]), 1)
            self.assertEqual(p2._recent_group_msgs["g1"][0]["id"], "m1")

    def test_expired_entries_dropped_on_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "recent_msgs.json"
            path.write_text(json.dumps({"g1": [
                {"id": "old", "text": "x", "ts": time.time() - 3600, "sender": "u1"},
                {"id": "new", "text": "y", "ts": time.time(), "sender": "u1"},
            ]}), encoding="utf-8")
            p = _plugin(d)
            p._load_recent_msgs()
            ids = [r["id"] for r in p._recent_group_msgs["g1"]]
            self.assertEqual(ids, ["new"])

    def test_corrupt_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "recent_msgs.json").write_text("{not json", encoding="utf-8")
            p = _plugin(d)
            p._load_recent_msgs()          # 不应抛异常
            self.assertEqual(p._recent_group_msgs, {})


if __name__ == "__main__":
    unittest.main()
