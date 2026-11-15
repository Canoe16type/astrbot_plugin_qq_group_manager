"""回归测试：退群档案删除后不能被重复事件重建（墓碑 + 事件去重）。

线上问题：成员退群 → 平台下发事件 → 插桩层记录/建档 → 管理员「清理已退群」删掉 →
同一条退群事件被重投/补发 → 又被 `create_left` 建回来，看起来"名义删了实际没删"。
"""

import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

main_mod = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
reg = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.member_registry")
QQGroupManager = main_mod.QQGroupManager


class _Logger:
    def _noop(self, *a, **k):
        pass

    debug = info = warning = error = _noop


class TombstoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = reg.MemberRegistry(str(self.dir), _Logger())

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_creates_tombstone(self):
        self.store.seen("g", "m", name="小明")
        old_event = time.time() - 30
        self.assertTrue(self.store.delete("g", "m"))
        # 迟到的旧事件应被拦截
        self.assertTrue(self.store.is_tombstoned("g", "m", old_event))
        # 明显晚于清理时间：说明是又退了一次群，应正常处理
        self.assertFalse(self.store.is_tombstoned("g", "m", time.time() + 3600))

    def test_delete_left_tombstones_all(self):
        self.store.seen("g", "a")
        self.store.mark_left("g", "a")
        self.store.seen("g", "b")
        self.assertEqual(self.store.delete_left("g"), 1)
        self.assertTrue(self.store.is_tombstoned("g", "a", time.time() - 10))
        self.assertFalse(self.store.is_tombstoned("g", "b", time.time() - 10))

    def test_rejoin_clears_tombstone(self):
        self.store.seen("g", "m")
        self.store.delete("g", "m")
        self.assertTrue(self.store.is_tombstoned("g", "m", time.time() - 5))
        self.store.clear_tombstone("g", "m")
        self.assertFalse(self.store.is_tombstoned("g", "m", time.time() - 5))

    def test_tombstones_persist(self):
        self.store.seen("g", "m")
        self.store.delete("g", "m")
        again = reg.MemberRegistry(str(self.dir), _Logger())
        self.assertTrue(again.is_tombstoned("g", "m", time.time() - 5))


class EventDedupeTest(unittest.TestCase):
    def test_same_event_id_seen_once(self):
        p = object.__new__(QQGroupManager)
        p._member_events_seen = {}
        self.assertFalse(p._member_event_seen("e1"))
        self.assertTrue(p._member_event_seen("e1"))
        self.assertFalse(p._member_event_seen("e2"))


class LeaveHandlerSkipTest(unittest.TestCase):
    """档案已清理后，迟到的退群事件不应再重建档案。"""

    def test_tombstoned_leave_event_skips_create(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = reg.MemberRegistry(tmp.name, _Logger())
        store.seen("g", "m", name="小明")
        store.delete("g", "m")                 # 管理员清理 + 墓碑

        p = object.__new__(QQGroupManager)
        p.logger = _Logger()
        p.members = store
        p._member_events_seen = {}
        p._group_in_scope = lambda g, pid=None: True
        p._members_on = lambda: True
        p._read_config_value = lambda *a, **k: False

        asyncio.run(p._on_group_member_remove({
            "group_openid": "g", "member_openid": "m",
            "timestamp": time.time() - 20, "_event_id": "evt-1",
        }))
        self.assertIsNone(store.get("g", "m"), "迟到的退群事件不应把档案建回来")

    def test_event_key_scoped_by_group_and_member(self):
        """平台会跨群复用同一个 event_id：不同群/不同成员不能互相去重。"""
        k1 = QQGroupManager._member_event_key("g1", "m1", {"_event_id": "same"})
        k2 = QQGroupManager._member_event_key("g2", "m1", {"_event_id": "same"})
        k3 = QQGroupManager._member_event_key("g1", "m2", {"_event_id": "same"})
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, k3)

    def test_dedupe_allows_same_id_in_other_group(self):
        p = object.__new__(QQGroupManager)
        p._member_events_seen = {}
        a = QQGroupManager._member_event_key("g1", "m", {"_event_id": "x"})
        b = QQGroupManager._member_event_key("g2", "m", {"_event_id": "x"})
        self.assertFalse(p._member_event_seen(a))
        self.assertFalse(p._member_event_seen(b), "另一个群的同 id 事件不能被当成重复")
        self.assertTrue(p._member_event_seen(a))


    def test_auto_delete_on_leave(self):
        """开启 auto_delete_on_leave 后，退群即删档并写墓碑。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = reg.MemberRegistry(tmp.name, _Logger())
        store.seen("g", "m", name="小明")

        p = object.__new__(QQGroupManager)
        p.logger = _Logger()
        p.members = store
        p._member_events_seen = {}
        p._group_in_scope = lambda g, pid=None: True
        p._members_on = lambda: True

        def _cfg(keys, default="", legacy_keys=()):
            k = keys[1] if isinstance(keys, tuple) and len(keys) > 1 else keys
            if k == "auto_delete_on_leave":
                return True
            if k == "member_remove_try_fetch_name":
                return False
            if k == "enable_member_remove_notify":
                return False
            return default
        p._read_config_value = _cfg

        asyncio.run(p._on_group_member_remove({
            "group_openid": "g", "member_openid": "m",
            "timestamp": time.time(), "_event_id": "evt-auto",
        }))
        self.assertIsNone(store.get("g", "m"), "开启自动删档后档案应被删除")
        self.assertTrue(store.is_tombstoned("g", "m", time.time() - 5))


if __name__ == "__main__":
    unittest.main()
