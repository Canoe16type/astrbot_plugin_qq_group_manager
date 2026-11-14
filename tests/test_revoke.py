"""P1 回归测试：处罚撤销（指令 + 自动解禁 + 历史排除）。

核心约束：
- 只能撤本群的、真的处罚过的记录；重复撤销幂等；
- 解禁以接口真值为准（本地记录会漂移）；
- 群管理员不能撤销针对自己的处罚，机器人管理员可以；
- 撤销后该记录永久留档，但不再计入「前科」。
"""

import asyncio
import importlib
import sys
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path

import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

QQGroupManager = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.main"
).QQGroupManager
moderation = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.moderation")


class _Logger:
    def _noop(self, *a, **k):
        pass

    debug = info = warning = error = _noop


class _FakeApi:
    def __init__(self, muted_first=True, raise_on_get=False):
        self.muted_first = muted_first
        self.raise_on_get = raise_on_get
        self.calls = []

    async def get_mute_state(self, group):
        self.calls.append(("get", group))
        if self.raise_on_get:
            raise RuntimeError("11253 无权限")
        if self.muted_first:
            self.muted_first = False          # 第一次在禁言中，第二次（复查）已解除
            return {"members": [{"member_openid": "u-target"}]}
        return {"members": []}

    async def set_mute(self, group, member, mute_expire_at=None, op="add"):
        self.calls.append(("set", op, member))
        return {"ok": True}


def _plugin(api=None):
    p = object.__new__(QQGroupManager)
    p.logger = _Logger()
    p.tmp = tempfile.TemporaryDirectory()
    p.mod = moderation.ModerationStore(p.tmp.name, p.logger)
    p.context = SimpleNamespace(get_config=lambda: {"admins_id": []})
    p._read_config_value = lambda keys, default="", legacy_keys=(): default
    p._api_for_group_or_default = lambda g, *a, **k: api or _FakeApi()
    p._mark_unmuted = lambda g, m: None
    p.replies = []
    p._reply = lambda event, text: _cap(p.replies, text)
    return p


def _cap(box, text):
    box.append(text)

    async def _done():
        return None

    return _done()


def _event(sender="u-admin", name="群主", role="owner", group="g1"):
    return SimpleNamespace(
        get_group_id=lambda: group,
        get_sender_id=lambda: sender,
        get_sender_name=lambda: name,
        get_platform_id=lambda: "qq_official",
        role=role,
    )


def _rec(p, **over):
    base = {"group": "g1", "target": "u-target", "target_name": "小明",
            "violation": True, "executed": True, "action": "mute",
            "ban_seconds": 600, "severity": "medium", "text": "广告",
            "created_at": int(time.time())}
    base.update(over)
    return p.mod.add_verdict(base)


class RevokeCoreTest(unittest.TestCase):
    def test_not_found_and_cross_group(self):
        p = _plugin()
        r = asyncio.run(p._revoke_verdict("g1", 999, operator="u-admin"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "not_found")

        rec = _rec(p, group="g2")
        r = asyncio.run(p._revoke_verdict("g1", rec["id"], operator="u-admin"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "cross_group")

    def test_not_punished(self):
        p = _plugin()
        rec = _rec(p, violation=False, executed=False, action="none", ban_seconds=0)
        r = asyncio.run(p._revoke_verdict("g1", rec["id"], operator="u-admin"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "not_punished")

    def test_unmute_when_still_muted(self):
        api = _FakeApi(muted_first=True)
        p = _plugin(api)
        rec = _rec(p)
        r = asyncio.run(p._revoke_verdict("g1", rec["id"], operator="u-admin", reason="误判"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["unmute"]["ok"])
        self.assertIn(("set", "del", "u-target"), api.calls)
        saved = p.mod.get_verdict(rec["id"])
        self.assertTrue(saved["revoked"]["unmute_ok"])
        self.assertEqual(saved["revoked"]["reason"], "误判")

    def test_unmute_when_already_clear(self):
        api = _FakeApi(muted_first=False)
        p = _plugin(api)
        rec = _rec(p)
        r = asyncio.run(p._revoke_verdict("g1", rec["id"], operator="u-admin"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["unmute"]["ok"])
        self.assertNotIn(("set", "del", "u-target"), api.calls)   # 不在禁言中就不调 del

    def test_unmute_api_error_is_recorded(self):
        api = _FakeApi(raise_on_get=True)
        p = _plugin(api)
        rec = _rec(p)
        r = asyncio.run(p._revoke_verdict("g1", rec["id"], operator="u-admin"))
        self.assertTrue(r["ok"])              # 撤销本身成功
        self.assertFalse(r["unmute"]["ok"])   # 但解禁失败要如实记录
        self.assertIn("11253", r["unmute"]["code"])
        self.assertFalse(p.mod.get_verdict(rec["id"])["revoked"]["unmute_ok"])

    def test_revoked_excluded_from_history(self):
        p = _plugin()
        rec = _rec(p)
        self.assertEqual(p.mod.repeat_count("g1", "u-target", window_days=30), 1)
        asyncio.run(p._revoke_verdict("g1", rec["id"], operator="u-admin"))
        self.assertEqual(p.mod.repeat_count("g1", "u-target", window_days=30), 0)

    def test_idempotent(self):
        p = _plugin()
        rec = _rec(p)
        asyncio.run(p._revoke_verdict("g1", rec["id"], operator="u-admin"))
        r = asyncio.run(p._revoke_verdict("g1", rec["id"], operator="u-admin"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["already"])


class RevokeCommandTest(unittest.TestCase):
    def test_group_admin_cannot_revoke_self(self):
        p = _plugin()
        rec = _rec(p, target="u-admin")      # 被处罚的就是操作人自己
        p._is_admin = lambda e: True
        asyncio.run(p._cmd_revoke(_event(sender="u-admin", role="owner"), ["/撤销", str(rec["id"])]))
        self.assertIn("不能撤销针对自己", "".join(p.replies))
        self.assertNotIn("revoked", p.mod.get_verdict(rec["id"]))

    def test_bot_admin_can_revoke_self(self):
        p = _plugin()
        rec = _rec(p, target="u-admin")
        p._is_admin = lambda e: True
        p._is_bot_admin_only = lambda e: True
        asyncio.run(p._cmd_revoke(_event(sender="u-admin", role="admin"), ["/撤销", str(rec["id"]), "误判"]))
        self.assertIn("已撤销", "".join(p.replies))
        self.assertTrue(p.mod.get_verdict(rec["id"])["revoked"])

    def test_non_admin_rejected(self):
        p = _plugin()
        rec = _rec(p)
        p._is_admin = lambda e: False
        asyncio.run(p._cmd_revoke(_event(role="member"), ["/撤销", str(rec["id"])]))
        self.assertIn("只有群主", "".join(p.replies))

    def test_usage_without_id(self):
        p = _plugin()
        p._is_admin = lambda e: True
        asyncio.run(p._cmd_revoke(_event(), ["/撤销"]))
        self.assertIn("用法：/群管 撤销", "".join(p.replies))


if __name__ == "__main__":
    unittest.main()
