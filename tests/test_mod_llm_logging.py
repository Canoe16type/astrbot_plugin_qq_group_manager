"""回归测试：审核链路必须把「模型到底调没调、调了什么、结论是什么」写进插件日志。

背景：线上反馈「/举报 之后后台日志只有一行『收到举报』，看不出有没有调用 LLM、
模型分析了什么」。这里锁住三件事：
1. 正常调用 → INFO 里有 provider、模式、提示词规模、耗时、token、判定结论；
2. 「没调模型」的每条提前返回路径都要说明原因（模型层关闭/无 provider/每日上限）；
3. 日志不得把整段提示词打到 INFO（原文只在 DEBUG 出现）。

跑法（插件目录下）：
    python3 -m unittest tests.test_mod_llm_logging -v
"""

import asyncio
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

QQGroupManager = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.main"
).QQGroupManager


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
    input_other = 120
    input_cached = 0
    output = 45


class _Resp:
    def __init__(self, text):
        self.completion_text = text
        self.usage = _Usage()


class _Ctx:
    def __init__(self, reply_text, raise_exc=False):
        self.calls = []
        self._reply_text = reply_text
        self._raise = raise_exc

    async def llm_generate(self, **kw):
        self.calls.append(kw)
        if self._raise:
            raise RuntimeError("provider 挂了")
        return _Resp(self._reply_text)


class _Mod:
    def __init__(self, today_calls=0):
        self._today = today_calls

    def get_dim_names(self, group=""):
        return {"semantic": "语义违规", "vision": "图片违规"}

    def get_cat_names(self, group=""):
        return {"ad": "广告", "other": "其他"}

    def get_rules(self, group=""):
        return []

    def stats(self, group=""):
        return {"today_llm_calls": self._today}

    def cache_get(self, text, ttl=0):
        return None


def _plugin(settings=None, reply_text=None, today_calls=0, raise_exc=False):
    p = object.__new__(QQGroupManager)
    p.logger = _Logger()
    p.mod = _Mod(today_calls)
    p._mod_few_shot = ""
    p._mod_few_shot_base = ""
    p._mod_llm_sem = {}
    p._mod_llm_pending = {}
    p._mod_last_llm = {}
    base = {"enable_model": True, "enable_vision": True, "daily_budget": 0}
    base.update(settings or {})
    p._mod_settings = lambda g: dict(base)
    p._read_config_value = lambda keys, default="", legacy_keys=(): ""
    p.context = _Ctx(reply_text or '{"violation": true, "basis": "semantic", "category": "ad", '
                                 '"severity": "medium", "confidence": 0.91, "reason": "拉人进群发广告"}',
                     raise_exc=raise_exc)
    return p


_REP = {"text": "加我vx进群领福利", "sender": "u1", "sender_name": "测试用户", "images": []}


def _run(p, **kw):
    kw.setdefault("pid_override", "deepseek-chat")
    return asyncio.run(p._mod_review_llm(None, "g1", _REP, [], **kw))


class ModLlmLogTest(unittest.TestCase):
    def test_success_path_logs_provider_tokens_and_verdict(self):
        p = _plugin()
        v = _run(p)
        self.assertTrue(v["violation"])
        logs = p.logger.text()
        self.assertIn("调用模型 provider=deepseek-chat", logs)
        self.assertIn("模式=纯文本", logs)
        self.assertIn("调用完成", logs)
        self.assertIn("tokens入=120", logs)
        self.assertIn("判定结论 违规=True", logs)
        self.assertIn("类型=广告", logs)
        self.assertIn("置信度=0.91", logs)
        # 提示词与原始返回只应出现在 DEBUG，INFO 里不留整段原文
        self.assertIn("提示词预览", p.logger.text("debug"))
        self.assertNotIn("提示词预览", p.logger.text("info"))

    def test_model_layer_off_is_explained(self):
        p = _plugin(settings={"enable_model": False})
        self.assertIsNone(_run(p))
        self.assertIn("模型层已关闭，不调用模型", p.logger.text("debug"))
        self.assertEqual(p.context.calls, [])

    def test_no_provider_is_explained(self):
        p = _plugin()
        p._mod_provider_id = lambda *a, **k: _async("")
        self.assertIsNone(_run(p, pid_override=""))
        self.assertIn("找不到可用的对话模型", p.logger.text("warning"))
        self.assertEqual(p.context.calls, [])

    def test_daily_budget_is_explained(self):
        p = _plugin(settings={"daily_budget": 100}, today_calls=100)
        self.assertIsNone(_run(p))
        self.assertIn("已达当日模型判定次数上限", p.logger.text("warning"))
        self.assertEqual(p.context.calls, [])

    def test_call_failure_is_logged_with_timing(self):
        p = _plugin(raise_exc=True)
        self.assertIsNone(_run(p))
        logs = p.logger.text("warning")
        self.assertIn("调用异常 provider=deepseek-chat", logs)
        self.assertIn("耗时", logs)

    def test_bad_json_is_logged(self):
        p = _plugin(reply_text="我觉得没问题")
        self.assertIsNone(_run(p))
        self.assertIn("模型未返回合法 JSON", p.logger.text("warning"))

    def test_scene_tag_used(self):
        p = _plugin()
        _run(p, scene="巡检")
        self.assertIn("[巡检][模型] 调用模型", p.logger.text())


def _async(value):
    async def _c(*a, **k):
        return value

    return _c()


if __name__ == "__main__":
    unittest.main()
