"""入群申请：事件通道 + 消息按钮审批的离线单元测试。

跑法（在插件目录下）：
    python3 -m unittest discover -s tests -v

完全离线：QQApi 换成记录器，事件体手工构造。覆盖四件事：
1. 按钮数据结构（行数上限 / 同申请互斥 / id 唯一 / 权限位）；
2. 回调数据编解码（含被篡改的 data 必须被拒）；
3. 按钮点击 → 审批执行 → 被动回执的整条链路与各类拒绝分支；
4. 申请事件 → 缓存 → 合并推送的去重行为。
"""

import asyncio
import importlib
import sys
import time
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR.parent))
sys.path.insert(0, str(_PLUGIN_DIR))

_m = importlib.import_module(_PLUGIN_DIR.name + ".main")
_bk = importlib.import_module(_PLUGIN_DIR.name + ".button_kit")
QQGroupManager = _m.QQGroupManager
QQApiError = _m.QQApiError

GROUP = "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5"
MEMBER = "C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6"
ADMIN = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"
OTHER = "F1E2D3C4B5A6F1E2D3C4B5A6F1E2D3C4"
APPLY_AT = "2026-11-07T13:00:00+08:00"


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **kw: None


class _Api:
    """记录器：审批与发送都只记账，不联网。"""

    def __init__(self, fail_with=None, pending=None):
        self.calls = []
        self.fail_with = fail_with
        self.pending = pending or []

    async def approve_join_request(self, g, member, join_request_id, op="approve", reject_reason=None):
        if self.fail_with is not None:
            raise QQApiError("审批失败", code=self.fail_with)
        self.calls.append({"op": op, "group": g, "member": member,
                           "join_request_id": join_request_id, "reason": reject_reason})

    async def get_join_request_list(self, g, limit=20):
        self.calls.append({"op": "list", "group": g})
        return {"list": list(self.pending)}

    async def send_group_message(self, g, content, event_id="", msg_id="", markdown=False, keyboard=None):
        self.calls.append({"op": "send", "group": g, "content": content,
                           "event_id": event_id, "keyboard": keyboard})
        return {"ok": True}


class _Comp:
    def __init__(self, data, iid="INT_1"):
        self.data = data
        self.interaction_id = iid
        self.action_id = "qgm_a0"


class _Event:
    def __init__(self, comp, group=GROUP, sender=ADMIN):
        self._comps = [comp]
        self._group = group
        self._sender = sender

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def get_platform_id(self):
        return "qq_official_1"

    def get_messages(self):
        return self._comps

    def get_button_interaction(self):
        return self._comps[0]


def make_plugin(config=None, api=None, pending=None):
    class Stub(QQGroupManager):
        def _group_in_scope(self, group_openid, platform_id=None):
            return True

        def _api_for_group_or_default(self, group_openid, fallback_pid=None):
            return api

        def _get_api(self, platform_id):
            return api

        def _target_groups(self):
            return []

    p = Stub.__new__(Stub)
    base = {
        "approval": {
            "enable_join_request_notify": True,
            "notify_buttons": True,
            "button_permission": "admin",
            "button_max_requests": 3,
            "notify_auto_approved": True,
            "join_event_mode": "event_first",
            "poll_interval": 30,
            "poll_interval_fallback": 300,
        },
        "admin": {"admin_openids": [ADMIN]},
        "basic": {"enable_welcome": False},
        "templates": {},
    }
    for group, items in (config or {}).items():
        if isinstance(items, dict):
            base.setdefault(group, {}).update(items)
        else:
            base[group] = items
    p.config = base
    p.logger = _Logger()
    p.plugin_data_dir = str(_PLUGIN_DIR / "tests" / "_tmp")
    p._pending = {GROUP: list(pending or [])}
    p._notified = {}
    p._known_groups = {GROUP: "qq_official_1"}
    p._join_event_at = {}
    p._join_event_total = 0
    p._join_event_last = 0.0
    p._join_error_code = {}
    p._join_backoff = {}
    p._join_denied_logged = set()
    p._nick_cache = {}
    p._join_fresh = {}
    p._join_notify_tasks = {}
    p._join_welcomed = {}
    p._interaction_done = {}
    p._interaction_total = 0
    p._interaction_last = 0.0
    p._interaction_last_result = ""
    p.sent = []          # 主动消息记录
    p.replies = []       # 普通回复记录

    async def _send_group_proactive(group_openid, text, platform_id, at_openid="", keyboard=None):
        p.sent.append({"group": group_openid, "text": str(text), "keyboard": keyboard})
        return True

    async def _reply(event, text):
        p.replies.append(str(text))

    p._send_group_proactive = _send_group_proactive
    p._reply = _reply
    return p


def _req(member=MEMBER, username="小明", apply_at=APPLY_AT, jrid="JR_1"):
    return {"member_openid": member, "username": username, "apply_at": apply_at,
            "join_request_id": jrid}


class ButtonKitTest(unittest.TestCase):
    def test_callback_roundtrip(self):
        data = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        parsed = _bk.parse_join_action(data)
        self.assertEqual(parsed["op"], "a")
        self.assertEqual(parsed["member_openid"], MEMBER)
        self.assertEqual(parsed["apply_at"], APPLY_AT)

    def test_tampered_data_rejected(self):
        good = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        for bad in ["", "qgm:ja:1:", "qgm:ja:1:@@@@", good[:-4],
                    "astrbot:whatever", _bk.encode_join_action("x", MEMBER, APPLY_AT)]:
            self.assertIsNone(_bk.parse_join_action(bad), f"应拒绝: {bad!r}")

    def test_keyboard_shape(self):
        reqs = [_req(member=f"M{i}", apply_at=f"2026-11-07T13:0{i}:00+08:00") for i in range(2)]
        kb = _bk.join_request_keyboard(reqs, permission="admin", max_requests=3)
        rows = kb["content"]["rows"]
        self.assertEqual(len(rows), 2)
        approve, decline = rows[0]["buttons"]
        # 同一申请的两个按钮共用 group_id：官方按它做「点完变灰」的互斥
        self.assertEqual(approve["group_id"], decline["group_id"])
        self.assertNotEqual(approve["group_id"], rows[1]["buttons"][0]["group_id"])
        # id 在同一消息内必须唯一
        ids = [b["id"] for r in rows for b in r["buttons"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(approve["action"]["permission"], {"type": 1})

    def test_row_limit(self):
        reqs = [_req(member=f"M{i}", apply_at=f"2026-11-07T13:0{i}:00+08:00") for i in range(9)]
        kb = _bk.join_request_keyboard(reqs, max_requests=9)
        self.assertLessEqual(len(kb["content"]["rows"]), _bk.MAX_ROWS)

    def test_normalize_variants(self):
        kb = _bk.join_request_keyboard([_req()], max_requests=1)
        self.assertEqual(_bk.normalize_keyboard(kb), kb)
        self.assertIsNone(_bk.normalize_keyboard(None))
        self.assertIsNone(_bk.normalize_keyboard("not json"))


class ButtonInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def _click(self, plugin, api, data, iid="INT_1", sender=ADMIN):
        event = _Event(_Comp(data, iid), sender=sender)
        return await plugin._handle_button_interaction(event), api, plugin

    async def test_approve_by_admin(self):
        api = _Api()
        p = make_plugin(api=api, pending=[_req()])
        data = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        consumed, _, _ = await self._click(p, api, data)
        self.assertTrue(consumed)
        self.assertEqual(api.calls[0]["op"], "approve")
        self.assertEqual(p._pending[GROUP], [])          # 缓存已清理
        # 回执走被动消息（event_id 非空），不吃主动消息频控
        sends = [c for c in api.calls if c["op"] == "send"]
        self.assertEqual(len(sends), 1)
        self.assertTrue(sends[0]["event_id"])

    async def test_duplicate_click_idempotent(self):
        api = _Api()
        p = make_plugin(api=api, pending=[_req()])
        data = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        await self._click(p, api, data, iid="INT_X")
        first = len([c for c in api.calls if c["op"] == "approve"])
        await self._click(p, api, data, iid="INT_X")
        second = len([c for c in api.calls if c["op"] == "approve"])
        self.assertEqual((first, second), (1, 1), "同一 interaction 不应重复执行")

    async def test_stale_request(self):
        api = _Api()
        p = make_plugin(api=api, pending=[])   # 缓存里没有 = 已被处理
        data = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        await self._click(p, api, data)
        self.assertFalse([c for c in api.calls if c["op"] == "approve"])
        self.assertIn("已处理或已失效", str(api.calls[-1]["content"]))

    async def test_tampered_data(self):
        api = _Api()
        p = make_plugin(api=api, pending=[_req()])
        await self._click(p, api, "qgm:ja:1:伪造数据")
        self.assertFalse([c for c in api.calls if c["op"] == "approve"])
        self.assertEqual(p._interaction_last_result, "数据无效")

    async def test_foreign_button_ignored(self):
        api = _Api()
        p = make_plugin(api=api, pending=[_req()])
        consumed, _, _ = await self._click(p, api, "astrbot:other_plugin:1")
        self.assertFalse(consumed, "别人的按钮不该被本插件消费")

    async def test_everyone_mode_requires_whitelist(self):
        api = _Api()
        p = make_plugin(api=api, pending=[_req()],
                        config={"approval": {"button_permission": "everyone"}})
        data = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        await self._click(p, api, data, sender=OTHER)
        self.assertFalse([c for c in api.calls if c["op"] == "approve"])
        self.assertEqual(p._interaction_last_result, "无权限")
        # 名单内的人点同一按钮要能过
        p._pending[GROUP] = [_req()]
        await self._click(p, api, data, iid="INT_2", sender=ADMIN)
        self.assertTrue([c for c in api.calls if c["op"] == "approve"])

    async def test_group_out_of_scope_ignored(self):
        api = _Api()
        p = make_plugin(api=api, pending=[_req()])
        p._group_in_scope = lambda g, pid=None: False   # 模拟配置改过、群已移出生效范围
        data = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        await self._click(p, api, data)
        self.assertFalse([c for c in api.calls if c["op"] == "approve"])
        self.assertEqual(p._interaction_last_result, "群不在生效范围")

    async def test_decline_with_api_error(self):
        api = _Api(fail_with=11703)
        p = make_plugin(api=api, pending=[_req()])
        data = _bk.encode_join_action(_bk.OP_DECLINE, MEMBER, APPLY_AT)
        await self._click(p, api, data)
        self.assertTrue(p._interaction_last_result.startswith("失败"))
        sends = [c for c in api.calls if c["op"] == "send"]
        self.assertIn("失败", sends[-1]["content"])

    async def test_same_decision_for_command_and_button(self):
        """指令与按钮必须走同一份实现：都调接口、都删缓存、都清去重键。"""
        data = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        results = []
        for via in ("cmd", "button"):
            api = _Api()
            p = make_plugin(api=api, pending=[_req()])
            p._notified[GROUP] = {(MEMBER, APPLY_AT): time.time()}
            if via == "cmd":
                ok, _ = await p._apply_join_decision(None, GROUP, "qq_official_1", _req(), "approve")
            else:
                await self._click(p, api, data)
                ok = p._interaction_last_result.startswith("通过")
            results.append((ok, api.calls[0]["op"], len(p._pending[GROUP]),
                            len(p._notified.get(GROUP) or {})))
        self.assertEqual(results[0], results[1])


class JoinRequestEventTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = _Api()
        self.p = make_plugin(api=self.api)
        # 合并窗口调到 10ms，测试不必真等 2 秒
        self.p.JOIN_NOTIFY_DEBOUNCE_SECONDS = 0.01

    async def _emit(self, member=MEMBER, apply_at=APPLY_AT, jrid="JR_E", **extra):
        payload = {"group_openid": GROUP, "member_openid": member,
                   "username": extra.pop("username", "小明"), "apply_at": apply_at,
                   "join_request_id": jrid, "apply_source": "search"}
        payload.update(extra)
        await self.p._on_group_join_request(payload, "qq_official_1")
        await asyncio.sleep(0.05)

    async def test_event_lands_in_pending_and_notifies(self):
        await self._emit()
        self.assertEqual(len(self.p._pending[GROUP]), 1)
        self.assertEqual(len(self.p.sent), 1)
        self.assertIsNotNone(self.p.sent[0]["keyboard"], "默认应带按钮")
        self.assertEqual(self.p._join_event_total, 1)
        self.assertIn(GROUP, self.p._join_event_at)

    async def test_duplicate_event_only_notifies_once(self):
        await self._emit()
        await self._emit(jrid="JR_E2")
        self.assertEqual(len(self.p.sent), 1, "同一申请重复推送只该发一条")
        self.assertEqual(len(self.p._pending[GROUP]), 1)
        # 事件体字段更全，应覆盖缓存里的旧值
        self.assertEqual(self.p._pending[GROUP][0]["join_request_id"], "JR_E2")

    async def test_batch_merged_into_one_message(self):
        for i in range(3):
            await self.p._on_group_join_request(
                {"group_openid": GROUP, "member_openid": f"M{i}", "username": f"新人{i}",
                 "apply_at": f"2026-11-07T14:0{i}:00+08:00"}, "qq_official_1")
        self.assertEqual(len(self.p.sent), 0, "合并窗口内不应立刻发送")
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.p.sent), 1, "三条申请应合并成一条通知")

    async def test_auto_approved_skips_pending(self):
        await self._emit(member="M_AUTO", auto_approved={"strategy_id": "1234"})
        self.assertEqual(self.p._pending[GROUP], [], "自动通过的申请不该进待审批")
        self.assertEqual(len(self.p.sent), 1)
        self.assertIn("1234", self.p.sent[0]["text"])

    async def test_notify_disabled(self):
        p = make_plugin(api=self.api, config={"approval": {"enable_join_request_notify": False}})
        p.JOIN_NOTIFY_DEBOUNCE_SECONDS = 0.01
        await p._on_group_join_request(
            {"group_openid": GROUP, "member_openid": MEMBER, "username": "小明",
             "apply_at": APPLY_AT}, "qq_official_1")
        await asyncio.sleep(0.05)
        self.assertEqual(p.sent, [])
        self.assertEqual(len(p._pending[GROUP]), 1, "关通知也要进缓存，方便指令处理")

    async def test_poll_and_event_share_dedupe(self):
        await self._emit()
        before = len(self.p.sent)
        await self.p._notify_join_requests(GROUP, "qq_official_1",
                                          list(self.p._pending[GROUP]), source="轮询")
        self.assertEqual(len(self.p.sent), before, "两条通道共用去重表，不该重复推")


class PollModeTest(unittest.TestCase):
    def test_interval_by_mode(self):
        api = _Api()
        p = make_plugin(api=api)
        self.assertEqual(p._poll_interval_seconds(), 300)
        p.config["approval"]["join_event_mode"] = "interval"
        self.assertEqual(p._poll_interval_seconds(), 30)
        p.config["approval"]["poll_interval"] = 1        # 过小要兜底
        self.assertEqual(p._poll_interval_seconds(), 10)
        p.config["approval"]["join_event_mode"] = "event_first"
        p.config["approval"]["poll_interval_fallback"] = 5
        self.assertEqual(p._poll_interval_seconds(), 60, "兜底轮询下限 60 秒")

    def test_invalid_mode_falls_back(self):
        p = make_plugin(api=_Api(), config={"approval": {"join_event_mode": "nonsense"}})
        self.assertEqual(p._join_event_mode(), "event_first")

    def test_button_options_clamped(self):
        p = make_plugin(api=_Api(), config={"approval": {"button_max_requests": 99}})
        _on, _perm, limit = p._join_button_options()
        self.assertEqual(limit, _bk.MAX_ROWS)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ButtonHandlerWiringTest(unittest.TestCase):
    """回归：按钮点击必须由一个挂了 ButtonActionFilter 的 handler 接。

    背景：AstrBot 4.29 的唤醒检查阶段对按钮事件只放行挂了 ButtonActionFilter 的
    handler，其余 handler 一律 continue（core/pipeline/waking_check/stage.py）。
    开发期首版把认领写在 on_group_message（只有 EventMessageTypeFilter）里，
    于是点击只触发了客户端的 visited_label 和适配器的 on_interaction_result(0)，
    服务端一次都没执行：申请没通过、欢迎语没发、回执也没有。
    """

    def _my_handlers(self):
        from astrbot.core.star.star_handler import EventType, star_handlers_registry

        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=False
        )
        return [
            h
            for h in handlers
            if str(h.handler_module_path).startswith(_PLUGIN_DIR.name)
        ]

    def test_click_lands_on_button_action_handler(self):
        from astrbot.core.star.filter.button_action import ButtonActionFilter

        mine = self._my_handlers()
        self.assertTrue(mine, "插件应注册适配器消息 handler")
        matched = [
            h
            for h in mine
            if any(isinstance(f, ButtonActionFilter) for f in h.event_filters)
        ]
        self.assertTrue(matched, "必须有一个挂了 ButtonActionFilter 的 handler，否则点击会被丢掉")
        self.assertIn("on_button_action", [h.handler_name for h in matched])
        self.assertTrue(hasattr(QQGroupManager, "on_button_action"))

    def test_plain_handler_alone_would_be_dropped(self):
        """on_group_message 只有 EventMessageTypeFilter —— 按钮事件到不了这里。"""
        from astrbot.core.star.filter.button_action import ButtonActionFilter

        h = next(h for h in self._my_handlers() if h.handler_name == "on_group_message")
        self.assertFalse(
            any(isinstance(f, ButtonActionFilter) for f in h.event_filters),
            "on_group_message 不应依赖按钮 filter，否则普通消息就收不到了",
        )

    def test_channel_ready_requires_both_conditions(self):
        p = make_plugin()
        self.assertTrue(_m._BUTTON_ACTION_SUPPORTED, "本环境 AstrBot 应提供 filter.button_action")
        self.assertTrue(p._button_channel_ready())


class WelcomeAfterApproveTest(unittest.IsolatedAsyncioTestCase):
    async def _click_approve(self, p, api):
        data = _bk.encode_join_action(_bk.OP_APPROVE, MEMBER, APPLY_AT)
        event = _Event(_Comp(data, "INT_W"), sender=ADMIN)
        return await p._handle_button_interaction(event)

    async def test_welcome_folded_into_passive_receipt_when_proactive_fails(self):
        """主动消息推不上去时，欢迎语必须并进 passive 回执，不能静默丢掉。"""
        api = _Api()
        p = make_plugin(api=api, pending=[_req()], config={"basic": {"enable_welcome": True},
                              "templates": {"msg_welcome_after_approve": "🎉 欢迎 {username} 加入本群！"}})

        async def _proactive_fail(group_openid, text, platform_id, at_openid="", keyboard=None):
            p.sent.append({"group": group_openid, "text": str(text), "keyboard": keyboard})
            return False

        p._send_group_proactive = _proactive_fail
        self.assertTrue(await self._click_approve(p, api))
        self.assertEqual(api.calls[0]["op"], "approve")
        sends = [c for c in api.calls if c["op"] == "send"]
        self.assertEqual(len(sends), 1)
        self.assertTrue(sends[0]["event_id"], "回执必须走 passive（event_id=互动事件 id）")
        self.assertIn("已通过", sends[0]["content"])
        self.assertIn("欢迎", sends[0]["content"], "主动推送失败时欢迎语应合并进回执")
        self.assertEqual(p.replies, [], "按钮路径不能退回 event.send()——那条路没有 msg_id")
        self.assertIn(f"{GROUP}:{MEMBER}", p._join_welcomed, "合并发送后同样要记入欢迎去重表")

    async def test_welcome_uses_proactive_when_available(self):
        api = _Api()
        p = make_plugin(api=api, pending=[_req()], config={"basic": {"enable_welcome": True},
                              "templates": {"msg_welcome_after_approve": "🎉 欢迎 {username} 加入本群！"}})
        self.assertTrue(await self._click_approve(p, api))
        self.assertEqual(len(p.sent), 1)
        self.assertIn("欢迎", p.sent[0]["text"])
        sends = [c for c in api.calls if c["op"] == "send"]
        self.assertEqual(len(sends), 1)
        self.assertNotIn("欢迎", sends[0]["content"], "主动推送成功时不该再塞进回执")

    async def test_welcome_disabled_sends_receipt_only(self):
        api = _Api()
        p = make_plugin(api=api, pending=[_req()], config={"basic": {"enable_welcome": False}})
        self.assertTrue(await self._click_approve(p, api))
        self.assertEqual(p.sent, [])
        sends = [c for c in api.calls if c["op"] == "send"]
        self.assertEqual(len(sends), 1)
        self.assertNotIn("欢迎", sends[0]["content"])
