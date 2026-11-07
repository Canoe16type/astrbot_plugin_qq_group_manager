"""机器人被移出群（GROUP_DEL_ROBOT）→ 自动清理群数据的离线单元测试。

跑法（在插件目录下）：
    python3 -m unittest discover -s tests -v

全离线：bot_state 接口用桩替代，只校验「什么情况下清、清了哪些表、
接口查不了时会不会误删」。真实网关推不推事件得在沙箱群里联调。
"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR.parent))
sys.path.insert(0, str(_PLUGIN_DIR))

_m = importlib.import_module(_PLUGIN_DIR.name + ".main")
QQGroupManager = _m.QQGroupManager
QQApiError = _m.QQApiError
_moderation = importlib.import_module(_PLUGIN_DIR.name + ".moderation")
_member_registry = importlib.import_module(_PLUGIN_DIR.name + ".member_registry")

GROUP = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"
OTHER = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
MEMBER = "C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6"
PID = "qq_official_main"


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **kw: None


class _Api:
    """bot_state 桩：可返回数据、可返回指定错误。"""

    def __init__(self, info=None, err=None):
        self.info = info
        self.err = err
        self.calls = 0

    async def get_group_bot_state(self, group):
        self.calls += 1
        return self.info, self.err


def make_plugin(config=None, api=None, tmpdir=None):
    class Stub(QQGroupManager):
        def _plugin_data_dir(self):
            return tmpdir

        def _api_for_group_or_default(self, group_openid, fallback_pid=None):
            return api

        def _get_api(self, platform_id):
            return api

        def _members_on(self):
            return True

    p = Stub.__new__(Stub)
    p.logger = _Logger()
    p.config = config or {}
    p._apis = {}
    p._known_groups = {GROUP: PID, OTHER: PID}
    p._intro_done = {GROUP: "event"}
    p._intro_pending = {GROUP: 1.0}
    p._mute_history = {GROUP: [{"id": 1}]}
    p._recall_history = {GROUP: [{"id": 2}]}
    p._history_seq = 1
    p._recall_seq = 1
    p._join_welcomed = {f"{GROUP}:{MEMBER}": 1.0, f"{OTHER}:{MEMBER}": 1.0}
    p._nick_cache = {GROUP: {MEMBER: "小明"}}
    p._pending = {GROUP: [{"member": MEMBER}]}
    p._notified = {GROUP: {}}
    p._seen_requests = {GROUP: set()}
    p._join_error_code = {GROUP: 11703}
    p._join_backoff = {GROUP: 0.0}
    p._join_event_at = {GROUP: 1.0}
    p._join_fresh = {GROUP: []}
    p._join_notify_tasks = {}
    p._recent_group_msgs = {GROUP: [{"id": "m1"}]}
    p._member_perm_blocked = {GROUP}
    p._join_denied_logged = {GROUP}
    p._last_active_group = GROUP
    p.mod = _moderation.ModerationStore(tmpdir, _Logger())
    p.members = _member_registry.MemberRegistry(tmpdir, _Logger())
    p.members.seen(GROUP, MEMBER, name="小明")
    p.members.seen(OTHER, MEMBER, name="小红")
    return p


def cfg(purge=True):
    return {"basic": {"enable_group_del_purge": purge}}


def del_event(group=GROUP, op=MEMBER):
    data = {"timestamp": 1784570535, "group_openid": group, "op_member_openid": op}
    obj = type("E", (), {})
    e = obj()
    for k, v in data.items():
        setattr(e, k, v)
    e.event_id = "EVENT_ID_1"
    return e


class PurgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_mod(self, p):
        p.mod.set_rules(GROUP, [{"id": "1", "text": "刷屏"}])
        p.mod.set_rules(OTHER, [{"id": "1", "text": "广告"}])
        p.mod.set_settings(GROUP, {"enabled": True})
        p.mod.add_verdict({"group": GROUP, "member": MEMBER})
        p.mod.add_verdict({"group": OTHER, "member": MEMBER})
        p.mod.set_group_meta(GROUP, name="被踢的群")
        p.mod.set_group_meta(OTHER, name="还在的群")

    async def test_purge_removes_only_that_group(self):
        p = make_plugin(cfg(), _Api(info={"member_role": "member"}), self.tmp)
        self._seed_mod(p)
        summary = p._purge_group_data(GROUP, PID, reason="单测")

        self.assertNotIn(GROUP, p._known_groups)
        self.assertIn(OTHER, p._known_groups)
        self.assertNotIn(GROUP, p._intro_done)
        self.assertNotIn(GROUP, p._mute_history)
        self.assertNotIn(GROUP, p._recall_history)
        self.assertNotIn(f"{GROUP}:{MEMBER}", p._join_welcomed)
        self.assertIn(f"{OTHER}:{MEMBER}", p._join_welcomed)
        self.assertNotIn(GROUP, p.mod.rules)
        self.assertIn(OTHER, p.mod.rules)
        self.assertNotIn(GROUP, p.mod.settings)
        self.assertEqual([v["group"] for v in p.mod.verdicts], [OTHER])
        self.assertNotIn(GROUP, p.mod.group_meta)
        self.assertNotIn(GROUP, p.members.groups)
        self.assertIn(OTHER, p.members.groups)
        self.assertEqual(summary.get("members"), 1)
        self.assertEqual(summary.get("verdicts"), 1)
        self.assertEqual(p._last_active_group, "")

    def test_purge_persists_to_disk(self):
        p = make_plugin(cfg(), _Api(), self.tmp)
        self._seed_mod(p)
        p._purge_group_data(GROUP, PID, reason="单测")

        with open(os.path.join(self.tmp, "mod_rules.json"), encoding="utf-8") as f:
            rules = json.load(f)
        self.assertNotIn(GROUP, rules)
        with open(os.path.join(self.tmp, "members.json"), encoding="utf-8") as f:
            groups = (json.load(f) or {}).get("groups") or {}
        self.assertNotIn(GROUP, groups)
        with open(os.path.join(self.tmp, "known_groups.json"), encoding="utf-8") as f:
            known = json.load(f)
        self.assertEqual(list(known), [OTHER])

    async def test_event_purges_when_bot_state_says_absent(self):
        api = _Api(err=QQApiError("群不存在", code=11244, status=404))
        p = make_plugin(cfg(), api, self.tmp)
        self._seed_mod(p)

        await p._on_group_del_robot(del_event(), PID)

        self.assertEqual(api.calls, 1)
        self.assertNotIn(GROUP, p._known_groups)

    async def test_event_purges_when_bot_state_has_no_permission(self):
        """11253（仅白名单机器人可用）属于「查不了」，以事件为准照样清理。"""
        api = _Api(err=QQApiError("应用无接口访问权限", code=11253, status=403))
        p = make_plugin(cfg(), api, self.tmp)

        await p._on_group_del_robot(del_event(), PID)

        self.assertNotIn(GROUP, p._known_groups)

    async def test_event_purges_on_11293_not_a_member(self):
        """11293「鉴权失败，机器人非群成员」= 明确离群，事件路径同样要清。"""
        api = _Api(
            err=QQApiError("HTTP 400: 鉴权失败，机器人非群成员", code=11293, status=400)
        )
        p = make_plugin(cfg(), api, self.tmp)
        self._seed_mod(p)

        await p._on_group_del_robot(del_event(), PID)

        self.assertNotIn(GROUP, p._known_groups)
        self.assertNotIn(GROUP, p.mod.rules)

    async def test_event_skips_purge_when_still_in_group(self):
        api = _Api(info={"member_role": "admin", "joined_at": "2025-06-15T14:30:00+08:00"})
        p = make_plugin(cfg(), api, self.tmp)
        self._seed_mod(p)

        await p._on_group_del_robot(del_event(), PID)

        self.assertIn(GROUP, p._known_groups)
        self.assertIn(GROUP, p.mod.rules)

    async def test_config_off_keeps_data(self):
        api = _Api(info=None, err=QQApiError("群不存在", status=404))
        p = make_plugin(cfg(purge=False), api, self.tmp)

        await p._on_group_del_robot(del_event(), PID)

        self.assertIn(GROUP, p._known_groups)
        self.assertEqual(api.calls, 0)   # 关掉后连接口都不打

    async def test_presence_states(self):
        p = make_plugin(cfg(), _Api(info={"member_role": "member"}), self.tmp)
        self.assertEqual((await p._check_group_presence(GROUP, PID))[0], "in")

        p = make_plugin(cfg(), _Api(err=QQApiError("群不存在", status=404)), self.tmp)
        self.assertEqual((await p._check_group_presence(GROUP, PID))[0], "absent")

        p = make_plugin(cfg(), _Api(err=QQApiError("应用无接口访问权限", code=11253)), self.tmp)
        self.assertEqual((await p._check_group_presence(GROUP, PID))[0], "unknown")

        # 11293 = 「鉴权失败，机器人非群成员」：400 而非 404，是明确离群，必须判 absent
        p = make_plugin(
            cfg(),
            _Api(err=QQApiError("HTTP 400: 鉴权失败，机器人非群成员", code=11293, status=400)),
            self.tmp,
        )
        self.assertEqual((await p._check_group_presence(GROUP, PID))[0], "absent")

        # 只有文案、没有 code 时也要认出来
        p = make_plugin(cfg(), _Api(err=QQApiError("机器人非群成员", status=400)), self.tmp)
        self.assertEqual((await p._check_group_presence(GROUP, PID))[0], "absent")

        p = make_plugin(cfg(), _Api(err=QQApiError("网络请求失败: timeout")), self.tmp)
        self.assertEqual((await p._check_group_presence(GROUP, PID))[0], "unknown")

    async def test_purge_summary_text(self):
        text = _m._purge_summary_text({"members": 3, "rules": 1})
        self.assertIn("成员档案 3 条", text)
        self.assertIn("群规", text)
        self.assertEqual(_m._purge_summary_text(None), "无残留数据")


if __name__ == "__main__":
    unittest.main()
