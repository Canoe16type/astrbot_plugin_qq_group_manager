"""回归测试：嵌套引用（引用一条「本身是引用」的消息）不能把目标认错。

线上现象：举报 A 的消息（A 那条内容其实是 B 的引用），插件把待审核消息解析成了更里层的
B 的正文（「转人机」），发送者变成举报人自己，上下文也围着错的人取。

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
    def __init__(self, raw, group="g1", sender="reporter", sname="Matsi"):
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


class NestedResolveTest(unittest.TestCase):
    def test_target_is_outer_message_not_inner(self):
        p = _plugin()
        now = time.time()
        p._recent_group_msgs["g1"] = [
            # 里层「转人机」是举报人自己几分钟前说的
            {"id": "m-inner", "text": "转人机", "ts": now - 300, "sender": "reporter",
             "sender_name": "Matsi", "images": []},
            # 真正被举报的：独木舟那条「/群管 举报」
            {"id": "m-target", "text": "/群管 举报", "ts": now - 5, "sender": "u-du",
             "sender_name": "独木舟TYPE16", "images": []},
        ]
        rep = p._mod_resolve_reported(_Event(_Raw(NESTED)))
        self.assertTrue(rep["resolved"])
        self.assertEqual(rep["id"], "m-target")
        self.assertEqual(rep["sender"], "u-du")
        self.assertEqual(rep["sender_name"], "独木舟TYPE16")
        self.assertNotIn("转人机", rep["text"])


if __name__ == "__main__":
    unittest.main()
