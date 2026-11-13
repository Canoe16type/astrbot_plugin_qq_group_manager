"""回归测试：嵌套引用（引用一条「本身是引用」的消息）不能把目标认错。

线上现象（真实三步）：A 先发「转人机」→ B引用它发 `/群管 举报` → A 再引用
B那条举报指令，发 `/群管 举报 滥用举报功能`。插件把待审核目标解析成了**第 1 步**的
「转人机」（更里层），发送者变成 A，上下文也围着错误的那条取。

根因：适配器渲染引用摘要时把嵌套引用一起铺进来：

    === 消息 1 ===
    [消息内容]  <@bot>
    /群管 举报
    [消息类型] 引用消息
    [关联消息]
    --- 第1条 ---
        [消息内容] 转人机

整段拿去匹配缓存，命中更里层的「转人机」。

跑法（插件目录下）：
    python3 -m unittest tests.test_quote_nested -v
"""

import importlib
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

QQGroupManager = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.main"
).QQGroupManager

NESTED = """=== 消息 1 ===
[消息内容]  <@52677D8D7866A8C0DCFBD970B781F571>
/群管 举报
[消息类型] 引用消息
[关联消息]
--- 第1条 ---
    [消息内容] 转人机
    [消息类型] 引用消息"""


class _Logger:
    def __init__(self):
        self.lines = []

    def _add(self, lv, m):
        self.lines.append((lv, str(m)))

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


class _Raw:
    def __init__(self, content, message_type=103):
        self.message_type = message_type
        self.msg_elements = [{"content": content}]
        self.id = "quote-evt"
        self.message_scene = {}
        self.message_reference = {}
        self.attachments = []


class _Event:
    def __init__(self, raw, group="g1", sender="reporter", sname="A"):
        self.message_obj = SimpleNamespace(message=[], raw_message=raw, message_id="evt-1")
        self._g, self._s, self._n = group, sender, sname

    def get_group_id(self):
        return self._g

    def get_sender_id(self):
        return self._s

    def get_sender_name(self):
        return self._n


def _plugin():
    p = object.__new__(QQGroupManager)
    p.logger = _Logger()
    p._recent_group_msgs = {}
    return p


class CleanSummaryTest(unittest.TestCase):
    def test_nested_quote_keeps_outer_message_only(self):
        out = QQGroupManager._clean_quote_summary(NESTED)
        self.assertIn("/群管 举报", out)
        self.assertNotIn("转人机", out)
        self.assertNotIn("[关联消息]", out)
        self.assertNotIn("<@", out)

    def test_forward_summary_untouched(self):
        """合并转发摘要没有嵌套标记，必须原样保留，否则 _quote_forward_items 解析不出来。"""
        text = "[消息内容] 第一句\n[消息内容] 第二句"
        self.assertEqual(QQGroupManager._clean_quote_summary(text), text)

    def test_empty_and_plain(self):
        self.assertEqual(QQGroupManager._clean_quote_summary(""), "")
        self.assertEqual(QQGroupManager._clean_quote_summary("转人机"), "转人机")


class TieBreakTest(unittest.TestCase):
    def test_same_text_prefers_most_recent_and_flags_ambiguous(self):
        """高频短句被多人说过：同分时取最近那条，并打 warning 说明存在歧义。"""
        p = _plugin()
        now = time.time()
        p._recent_group_msgs["g1"] = [
            {"id": "old", "text": "哈哈", "ts": now - 200, "sender": "u1",
             "sender_name": "A", "images": []},
            {"id": "new", "text": "哈哈", "ts": now - 2, "sender": "u2",
             "sender_name": "B", "images": []},
        ]
        rep = p._mod_resolve_reported(_Event(_Raw("哈哈", message_type=0)))
        self.assertEqual(rep["id"], "new")
        self.assertEqual(rep["sender"], "u2")
        self.assertTrue(rep.get("match_ambiguous"))
        self.assertIn("同分候选", p.logger.text("warning"))

    def test_ambiguous_never_executes(self):
        """同文候选多条时选谁都可能错：即使判定违规，也只能判定不能处罚。"""
        p = _plugin()
        p.mod = SimpleNamespace(get_rules=lambda g: [])
        p._read_config_value = lambda keys, default="", legacy_keys=(): default
        import asyncio
        res = asyncio.run(p._mod_execute(
            SimpleNamespace(get_platform_id=lambda: "qq_official"), "g1",
            {"match_ambiguous": True, "sender": "u2"},
            {"violation": True, "basis": "semantic", "category": "ad", "severity": "high",
             "confidence": 0.99, "reason": "广告"},
            {"dry_run": False, "max_mute_seconds": 60},
        ))
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["ambiguous"])
        self.assertFalse(res["executed"])


class UnattributedTest(unittest.TestCase):
    def test_old_message_keeps_content_but_marks_unattributed(self):
        """引用超过缓存窗口：正文还在，但要如实标记「没定位到发送者」。"""
        p = _plugin()
        p._recent_group_msgs["g1"] = []
        rep = p._mod_resolve_reported(_Event(_Raw("这人好烦啊", message_type=0)))
        self.assertFalse(rep["resolved"])
        self.assertTrue(rep["unattributed"])
        self.assertEqual(rep["text"], "这人好烦啊")
        self.assertEqual(rep["sender"], "")

    def test_unattributed_never_executes(self):
        """未定位发送者时，即使判定违规且 dry_run 关闭，也只能判定不能处罚。"""
        p = _plugin()
        p.mod = SimpleNamespace(get_rules=lambda g: [])
        p._read_config_value = lambda keys, default="", legacy_keys=(): default
        res = __import__("asyncio").run(p._mod_execute(
            SimpleNamespace(get_platform_id=lambda: "qq_official"),
            "g1",
            {"unattributed": True, "sender": ""},
            {"violation": True, "basis": "semantic", "category": "ad", "severity": "medium",
             "confidence": 0.99, "reason": "广告"},
            {"dry_run": False, "max_mute_seconds": 60},
        ))
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["unattributed"])
        self.assertFalse(res["executed"])
        self.assertIn("无法确定", res["skipped_reason"])


DEEP_CHAIN = """=== 消息 1 ===
[消息内容] 7
[消息类型] 引用消息
[关联消息]
--- 第1条 ---
    [消息内容] 6
    [消息类型] 引用消息
    [关联消息]
    --- 第1条 ---
        [消息内容] 5
        [消息类型] 引用消息
        [关联消息]
        --- 第1条 ---
            [消息内容] 4
            [消息类型] 引用消息
            [关联消息]
            --- 第1条 ---
                [消息内容] 3
                [消息类型] 引用消息
                [关联消息]
                --- 第1条 ---
                    [消息内容] 2
                    [消息类型] 引用消息
                    [关联消息]
                    --- 第1条 ---
                        [消息内容] 1
                        [消息类型] 引用消息"""


class DeepChainTest(unittest.TestCase):
    """7 层嵌套（1←2←…←7，消息 8 引用 7）：只能取最外层的 7，绝不能沉到底部的 1。"""

    def test_cleaner_takes_outermost_only(self):
        self.assertEqual(QQGroupManager._clean_quote_summary(DEEP_CHAIN), "7")

    def test_resolve_picks_directly_quoted_message(self):
        p = _plugin()
        now = time.time()
        senders = {1: "u-zhu", 2: "u-alt", 3: "u-zhu", 4: "u-alt",
                   5: "u-zhu", 6: "u-alt", 7: "u-zhu", 8: "u-alt"}
        names = {"u-zhu": "B2", "u-alt": "B"}
        p._recent_group_msgs["g1"] = [
            {"id": f"m{i}", "text": str(i), "ts": now - (9 - i),
             "sender": senders[i], "sender_name": names[senders[i]], "images": []}
            for i in range(1, 9)
        ]
        ev = _Event(_Raw(DEEP_CHAIN), sender="u-alt", sname="B")
        rep = p._mod_resolve_reported(ev)
        self.assertTrue(rep["resolved"])
        self.assertEqual(rep["text"], "7")
        self.assertEqual(rep["id"], "m7")
        self.assertEqual(rep["sender_name"], "B2")


class NestedResolveTest(unittest.TestCase):
    def test_target_is_outer_message_not_inner(self):
        p = _plugin()
        now = time.time()
        p._recent_group_msgs["g1"] = [
            # 里层「转人机」是举报人自己几分钟前说的
            {"id": "m-inner", "text": "转人机", "ts": now - 300, "sender": "reporter",
             "sender_name": "A", "images": []},
            # 真正被举报的：B那条「/群管 举报」（第 2 步）
            {"id": "m-target", "text": "/群管 举报", "ts": now - 5, "sender": "u-du",
             "sender_name": "BTYPE16", "images": []},
            # 第 3 步举报人自己刚发的那条，含相同前缀，不能把目标抢走
            {"id": "m-report", "text": "/群管 举报 滥用举报功能", "ts": now - 1,
             "sender": "reporter", "sender_name": "A", "images": []},
        ]
        rep = p._mod_resolve_reported(_Event(_Raw(NESTED)))
        self.assertTrue(rep["resolved"])
        self.assertEqual(rep["id"], "m-target")
        self.assertEqual(rep["sender"], "u-du")
        self.assertEqual(rep["sender_name"], "BTYPE16")
        self.assertNotIn("转人机", rep["text"])


if __name__ == "__main__":
    unittest.main()
