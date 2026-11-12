"""回归测试：判定记录留原文 / 模型调用闸门 / 反馈样本回流。

对应 1.5.0 的四项加固：
- 判定记录以前只留 200 字且页面不展示，人工没法二次确认；
- 自动巡检每条消息一次模型调用、无并发上限、每日预算默认不限；
- 误判样本只写进内存，重启即丢。

跑法（插件目录下）：
    python3 -m unittest tests.test_mod_hardening -v
"""

import asyncio
import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

QQGroupManager = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.main"
).QQGroupManager
moderation = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.moderation")


class _Logger:
    def __init__(self):
        self.lines = []

    def _add(self, lvl, msg):
        self.lines.append((lvl, str(msg)))

    def debug(self, m):
        self._add("debug", m)

    def info(self, m):
        self._add("info", m)

    def warning(self, m):
        self._add("warning", m)

    def error(self, m):
        self._add("error", m)

    def text(self, level=None):
        return "\n".join(m for lv, m in self.lines if level is None or lv == level)


class _Usage:
    input_other = 10
    input_cached = 0
    output = 5


class _Resp:
    completion_text = '{"violation": true, "basis": "semantic", "category": "ad", ' \
                      '"severity": "high", "confidence": 0.9, "reason": "广告"}'
    usage = _Usage()


class _SlowCtx:
    """模拟 provider 卡住：耗时超过 llm_timeout。"""

    def __init__(self, delay):
        self.delay = delay
        self.calls = 0

    async def llm_generate(self, **kw):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return _Resp()


class _FastCtx:
    def __init__(self):
        self.calls = 0

    async def llm_generate(self, **kw):
        self.calls += 1
        return _Resp()


class _ModStub:
    def __init__(self, today_calls=0, samples=None):
        self._today = today_calls
        self._samples = samples or {}

    def get_dim_names(self, group=""):
        return {"semantic": "语义违规"}

    def get_cat_names(self, group=""):
        return {"ad": "广告"}

    def get_rules(self, group=""):
        return []

    def stats(self, group=""):
        return {"today_llm_calls": self._today}

    def cache_get(self, text, ttl=0):
        return None

    def feedback_samples(self, limit=20):
        return self._samples


def _plugin(settings=None, ctx=None, today_calls=0, samples=None):
    p = object.__new__(QQGroupManager)
    p.logger = _Logger()
    p.mod = _ModStub(today_calls, samples)
    p._mod_few_shot = ""
    p._mod_few_shot_base = ""
    p._mod_llm_sem = {}
    p._mod_llm_pending = {}
    p._mod_last_llm = {}
    base = {"enable_model": True, "enable_vision": True, "daily_budget": 0,
            "llm_queue_limit": 3, "llm_timeout": 45}
    base.update(settings or {})
    p._mod_settings = lambda g: dict(base)
    p._read_config_value = lambda keys, default="", legacy_keys=(): ""
    p.context = ctx or _FastCtx()
    return p


_REP = {"text": "加我vx进群领福利", "sender": "u1", "sender_name": "测试用户", "images": []}


class VerdictTextTest(unittest.TestCase):
    def test_keeps_full_text_up_to_cap(self):
        p = _plugin()
        long_text = "违" * 1500
        out = p._verdict_text({"text": long_text, "images": ["a", "b"]})
        self.assertEqual(len(out["text"]), 1000)
        self.assertEqual(out["text_len"], 1500)
        self.assertTrue(out["text_truncated"])
        self.assertEqual(out["images"], 2)

    def test_short_text_not_truncated(self):
        p = _plugin()
        out = p._verdict_text({"text": "短的", "images": []})
        self.assertEqual(out["text"], "短的")
        self.assertFalse(out["text_truncated"])
        self.assertEqual(out["images"], 0)


class GateTest(unittest.TestCase):
    def test_queue_full_skips_model(self):
        p = _plugin()
        p._mod_llm_pending["g1"] = 3          # 已达 llm_queue_limit=3
        v = asyncio.run(p._mod_review_llm(None, "g1", _REP, [], pid_override="prov"))
        self.assertIsNone(v)
        self.assertEqual(p.context.calls, 0)
        self.assertIn("排队已满", p.logger.text("info"))

    def test_queue_under_limit_still_calls(self):
        p = _plugin()
        p._mod_llm_pending["g1"] = 2
        v = asyncio.run(p._mod_review_llm(None, "g1", _REP, [], pid_override="prov"))
        self.assertTrue(v["violation"])
        self.assertEqual(p.context.calls, 1)
        self.assertEqual(p._mod_llm_pending["g1"], 2)   # 计数必须归还原位

    def test_timeout_fails_fast_and_releases_slot(self):
        p = _plugin(settings={"llm_timeout": 0.05}, ctx=_SlowCtx(delay=0.5))
        v = asyncio.run(p._mod_review_llm(None, "g1", _REP, [], pid_override="prov"))
        self.assertIsNone(v)
        self.assertIn("调用超时", p.logger.text("warning"))
        self.assertEqual(p._mod_llm_pending.get("g1", 0), 0)   # 超时不能把队列卡死
        self.assertFalse(p._mod_last_llm.get("ok"))

    def test_last_call_recorded_for_selfcheck(self):
        p = _plugin()
        asyncio.run(p._mod_review_llm(None, "g1", _REP, [], pid_override="prov"))
        self.assertTrue(p._mod_last_llm["ok"])
        self.assertEqual(p._mod_last_llm["provider"], "prov")
        self.assertIn("ms", p._mod_last_llm)


class FeedbackSampleTest(unittest.TestCase):
    def test_store_returns_labelled_samples(self):
        with tempfile.TemporaryDirectory() as d:
            store = moderation.ModerationStore(Path(d), _Logger())
            store.add_verdict({"group": "g", "text": "误判原文", "feedback": "false_positive"})
            store.add_verdict({"group": "g", "text": "正例原文", "feedback": "true_positive",
                               "category": "ad", "severity": "high"})
            store.add_verdict({"group": "g", "text": "没标注"})
            out = store.feedback_samples(limit=20)
            self.assertEqual([x["text"] for x in out["false_positive"]], ["误判原文"])
            self.assertEqual([x["text"] for x in out["true_positive"]], ["正例原文"])

    def test_rebuild_injects_persisted_samples(self):
        samples = {
            "false_positive": [{"text": "这句没问题", "category": "", "basis": "semantic", "severity": ""}],
            "true_positive": [{"text": "加群领红包", "category": "ad", "basis": "semantic", "severity": "high"}],
        }
        p = _plugin(samples=samples)
        p._mod_few_shot_base = "- 内置示例\n"
        p._rebuild_mod_few_shot()
        self.assertIn("内置示例", p._mod_few_shot)
        self.assertIn("这句没问题", p._mod_few_shot)
        self.assertIn("violation=false", p._mod_few_shot)
        self.assertIn("加群领红包", p._mod_few_shot)
        self.assertIn("category=ad", p._mod_few_shot)

    def test_rebuild_survives_broken_store(self):
        p = _plugin()

        def boom(limit=20):
            raise RuntimeError("数据文件坏了")

        p.mod.feedback_samples = boom
        p._mod_few_shot_base = "- 内置示例\n"
        p._rebuild_mod_few_shot()          # 不能让坏文件挡住启动
        self.assertIn("内置示例", p._mod_few_shot)


if __name__ == "__main__":
    unittest.main()


async def _reply_capture(box, text):
    box.append(text)


class SelfCheckTest(unittest.TestCase):
    def test_selfcheck_reports_layers_provider_and_budget(self):
        p = _plugin(settings={"daily_budget": 500})
        p._mod_provider_id = lambda *a, **k: _ared("prov-x")
        box = []
        p._reply = lambda event, text: _reply_capture(box, text)
        event = SimpleNamespace(get_group_id=lambda: "g1",
                                get_platform_id=lambda: "qq_official")
        asyncio.run(p._cmd_selfcheck(event))
        out = "\n".join(box)
        self.assertIn("适配器：qq_official", out)
        self.assertIn("审核模型：prov-x", out)
        self.assertIn("预算 500", out)
        self.assertIn("并发闸门", out)
        self.assertIn("不做主动探活", out)


class ExportTest(unittest.TestCase):
    def test_export_filters_group_and_respects_limit(self):
        main_mod = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
        with tempfile.TemporaryDirectory() as d:
            store = moderation.ModerationStore(Path(d), _Logger())
            store.add_verdict({"group": "g1", "text": "a", "created_at": int(time.time())})
            store.add_verdict({"group": "g2", "text": "b", "created_at": int(time.time())})
            p = _plugin()
            p.mod = store

            class _Q(dict):
                def get(self, k, default=None, type=None):
                    v = dict.get(self, k, default)
                    return type(v) if type and v is not None else v

            class _R:
                query = _Q({"group": "g1", "limit": 10})

            old = main_mod.request
            main_mod.request = _R()
            try:
                raw = asyncio.run(p.web_mod_verdicts_export())
            finally:
                main_mod.request = old
            out = json.loads(getattr(raw, "body", raw))
            self.assertEqual(out["total"], 1)
            self.assertEqual(out["items"][0]["group"], "g1")


async def _ared(v):
    return v
