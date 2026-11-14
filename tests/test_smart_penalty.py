"""P2 回归测试：智能分级处罚（开关默认关）。

契约：
- 关闭时行为与旧版一致（medium → 撤回 + 禁言）；
- 开启后 low→警告(不撤回)、medium→撤回+警告、high→禁言；
- high 但置信度不足 → 转人工待办，不自动处罚；
- 命中群规时长优先，模型依旧不能指定秒数。
"""

import asyncio
import importlib
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

main_mod = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
moderation = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.moderation")
QQGroupManager = main_mod.QQGroupManager
decide_action = moderation.decide_action


def _verdict(sev="low", conf=0.9, cat="abuse", rid=""):
    return {"violation": True, "severity": sev, "confidence": conf, "category": cat,
            "basis": "semantic", "reason": "测试理由", "matched_rule_id": rid}


class DecideActionSmartTest(unittest.TestCase):
    def test_low_is_warn_without_recall(self):
        d = decide_action(_verdict("low"), rules=[], smart=True)
        self.assertEqual(d["action"], "warn")
        self.assertFalse(d["recall"])
        self.assertEqual(d["seconds"], 0)

    def test_medium_is_warn_with_recall(self):
        d = decide_action(_verdict("medium"), rules=[], smart=True)
        self.assertEqual(d["action"], "warn")
        self.assertTrue(d["recall"])

    def test_high_is_mute(self):
        d = decide_action(_verdict("high", 0.9), rules=[], smart=True)
        self.assertEqual(d["action"], "mute")
        self.assertGreaterEqual(d["seconds"], 60)

    def test_high_low_confidence_goes_todo(self):
        d = decide_action(_verdict("high", 0.5), rules=[], smart=True)
        self.assertEqual(d["action"], "todo")
        self.assertIn("人工", d["skipped_reason"])

    def test_rule_seconds_win_even_when_smart(self):
        rules = [{"id": "r1", "name": "广告", "enabled": True, "category": "ad",
                  "severity": "medium", "seconds": 1800}]
        d = decide_action(_verdict("medium", 0.9, rid="r1"), rules=rules, smart=True)
        self.assertEqual(d["action"], "mute")
        self.assertEqual(d["seconds"], 1800)

    def test_smart_off_keeps_old_behavior(self):
        d = decide_action(_verdict("medium"), rules=[], smart=False)
        self.assertEqual(d["action"], "mute")


class PriorTierTest(unittest.TestCase):
    def test_low_with_prior_bumps_to_recall(self):
        d = decide_action(_verdict("low", 0.9), rules=[], smart=True, prior_offenses=2)
        self.assertEqual(d["action"], "warn")
        self.assertTrue(d["recall"])

    def test_medium_with_prior_bumps_to_mute(self):
        d = decide_action(_verdict("medium", 0.9), rules=[], smart=True, prior_offenses=2)
        self.assertEqual(d["action"], "mute")

    def test_bump_can_be_disabled_by_threshold(self):
        d = decide_action(_verdict("low", 0.9), rules=[], smart=True,
                          prior_offenses=1, prior_bump_at=5)
        self.assertFalse(d["recall"])


class _Api:
    def __init__(self):
        self.calls = []

    async def recall_group_message(self, group, mid):
        self.calls.append(("recall", mid))

    async def set_mute(self, group, member, mute_expire_at=None, op="add"):
        self.calls.append(("mute", member))


def _plugin(api, prior=0):
    p = object.__new__(QQGroupManager)
    p.logger = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                               warning=lambda *a, **k: None, error=lambda *a, **k: None)
    p.mod = SimpleNamespace(get_rules=lambda g: [], repeat_count=lambda *a, **k: prior)
    p._read_config_value = lambda keys, default="", legacy_keys=(): default
    p._mod_settings = lambda g: dict(SETTINGS)
    p._api_for_group_or_default = lambda g, **k: api
    p._is_qq_admin_openid = lambda g, m: False
    p._add_recall_record = lambda **k: None
    p._add_mute_record = lambda **k: None
    p.warnings = []
    p.replies = []

    async def _warn(event, group, rep, out, dec):
        p.warnings.append(dict(out))

    p._send_mod_warning = _warn
    p._reply = lambda e, t: _ok()
    return p


def _ok():
    async def _d():
        return None
    return _d()


def _event():
    return SimpleNamespace(get_platform_id=lambda: "qq_official",
                           get_sender_id=lambda: "u-reporter",
                           get_sender_name=lambda: "举报人")


def _rep():
    return {"id": "m1", "text": "内容", "sender": "u-target", "sender_name": "小明",
            "ts": time.time(), "images": []}


SETTINGS = {"dry_run": False, "confidence_threshold": 0.75, "max_mute_seconds": 86400,
            "recall_max_age_seconds": 110, "whitelist": [], "smart_penalty": True}


class ExecuteSmartTest(unittest.TestCase):
    def test_medium_warns_and_recalls_without_mute(self):
        api = _Api()
        p = _plugin(api)
        res = asyncio.run(p._mod_execute(_event(), "g1", _rep(), _verdict("medium", 0.9), SETTINGS))
        self.assertEqual(res["action"], "warn")
        self.assertTrue(res["executed"])
        self.assertIn(("recall", "m1"), api.calls)
        self.assertFalse(any(c[0] == "mute" for c in api.calls))
        self.assertEqual(len(p.warnings), 1)

    def test_low_warns_without_recall(self):
        api = _Api()
        p = _plugin(api)
        res = asyncio.run(p._mod_execute(_event(), "g1", _rep(), _verdict("low", 0.9), SETTINGS))
        self.assertEqual(res["action"], "warn")
        self.assertEqual(api.calls, [])

    def test_high_low_conf_todo_no_action(self):
        api = _Api()
        p = _plugin(api)
        res = asyncio.run(p._mod_execute(_event(), "g1", _rep(), _verdict("high", 0.5), SETTINGS))
        self.assertEqual(res["action"], "todo")
        self.assertTrue(res["pending_review"])
        self.assertFalse(res["executed"])
        self.assertEqual(api.calls, [])
        self.assertEqual(p.warnings, [])

    def test_prior_escalates_duration(self):
        cfg = dict(SETTINGS)
        cfg["max_mute_seconds"] = 604800      # 放宽上限，否则两次都撞到 86400 上限看不出差别
        p = _plugin(_Api(), prior=2)
        base = asyncio.run(p._mod_execute(_event(), "g1", _rep(), _verdict("high", 0.9), cfg))
        p2 = _plugin(_Api(), prior=0)
        plain = asyncio.run(p2._mod_execute(_event(), "g1", _rep(), _verdict("high", 0.9), cfg))
        self.assertEqual(base["action"], "mute")
        self.assertGreater(base["seconds"], plain["seconds"])
        self.assertGreater(base["escalation"], 1.0)

    def test_smart_off_still_mutes(self):
        api = _Api()
        p = _plugin(api)
        cfg = dict(SETTINGS)
        cfg["smart_penalty"] = False
        res = asyncio.run(p._mod_execute(_event(), "g1", _rep(), _verdict("medium", 0.9), cfg))
        self.assertEqual(res["action"], "mute")
        self.assertIn(("mute", "u-target"), api.calls)


if __name__ == "__main__":
    unittest.main()

class TemplateOverrideTest(unittest.TestCase):
    """新话术必须能按群覆盖（群规审核 → 话术模板），而不是只能改全局。"""

    def test_defaults_registered(self):
        for key in ("msg_mod_warn", "msg_mod_todo", "msg_mod_revoke"):
            self.assertIn(key, moderation.DEFAULT_TEMPLATES)

    def test_group_override_wins(self):
        p = _plugin(_Api())
        p.mod.templates = {"groups": {"g1": {"msg_mod_warn": "自定义警告：{rule}"}}}
        p.mod.get_templates = lambda g: {"msg_mod_warn": "自定义警告：{rule}"}
        text = p._mod_tpl("g1", "msg_mod_warn", at="", rule="群规3",
                          severity="轻微", reason="", next="5分钟")
        self.assertIn("自定义警告", text)
        self.assertIn("群规3", text)

    def test_falls_back_to_default(self):
        p = _plugin(_Api())
        p.mod.templates = {}
        p.mod.get_templates = lambda g: {}
        text = p._mod_tpl("g1", "msg_mod_todo")
        self.assertIn("人工复核", text)


if __name__ == "__main__":
    unittest.main()

class PredictActionTest(unittest.TestCase):
    """试跑/审核测试用的无副作用预测：必须与真实执行同参同逻辑。"""

    def test_low_warns_without_recall(self):
        p = _plugin(_Api())
        d = p._mod_predict_action("g1", _verdict("low", 0.9), _rep())
        self.assertEqual(d["action"], "warn")
        self.assertFalse(d["recall"])

    def test_high_conf_below_threshold_is_todo(self):
        p = _plugin(_Api())
        d = p._mod_predict_action("g1", _verdict("high", 0.7), _rep())   # 阈值 0.75
        self.assertEqual(d["action"], "todo")

    def test_prior_bumps_tier_and_duration(self):
        p = _plugin(_Api(), prior=2)
        p._mod_settings = lambda g: dict(SETTINGS, smart_penalty=True, max_mute_seconds=604800)
        d = p._mod_predict_action("g1", _verdict("medium", 0.9), _rep())
        self.assertEqual(d["action"], "mute")          # 中度 + 前科 → 升档禁言
        self.assertGreater(d["seconds"], 0)
        self.assertGreater(d["escalation"], 1.0)

    def test_smart_off_legacy_behavior(self):
        p = _plugin(_Api())
        p._mod_settings = lambda g: dict(SETTINGS, smart_penalty=False)
        d = p._mod_predict_action("g1", _verdict("medium", 0.9), _rep())
        self.assertEqual(d["action"], "mute")
        self.assertEqual(d["escalation"], 1.0)


if __name__ == "__main__":
    unittest.main()
