"""回归测试：举报引用消息里的图片必须能进图片维度审核（1.5.1）。

线上现象：引用一条「😶 + 图片」的消息举报，日志里 `模式=纯文本`，图片维度被静默跳过。
根因：QQ 官方群消息常把「文字 + 图片」拆成两条投递，按正文匹配到的是文本那条，
`rec["images"]` 为空。引用纯图片消息时更惨——摘要没有正文，连目标都定位不到。

跑法（插件目录下）：
    python3 -m unittest tests.test_mod_image_review -v
"""

import importlib
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

main_mod = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
QQGroupManager = main_mod.QQGroupManager


class _Logger:
    def __init__(self):
        self.lines = []

    def _add(self, lvl, m):
        self.lines.append((lvl, str(m)))

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
    def __init__(self, elements=None, attachments=None, message_type=103):
        self.message_type = message_type
        self.msg_elements = elements or []
        self.attachments = attachments or []
        self.id = "raw-1"


class _Event:
    def __init__(self, raw=None, message=None, group="g1", sender="u1", sname="A",
                 mid="quote-msg-1"):
        self.message_obj = SimpleNamespace(message=message or [], raw_message=raw, message_id=mid)
        self._g, self._sender, self._sname = group, sender, sname

    def get_group_id(self):
        return self._g

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._sname


def _plugin():
    p = object.__new__(QQGroupManager)
    p.logger = _Logger()
    p._recent_group_msgs = {}
    return p


class NearbyImagesTest(unittest.TestCase):
    def test_text_and_image_split_into_two_messages(self):
        """文本一条、图片一条：按正文匹配到文本那条，图片要从相邻缓存补回来。"""
        p = _plugin()
        now = time.time()
        p._recent_group_msgs["g1"] = [
            {"id": "m-text", "text": "😶", "ts": now - 1.2, "sender": "u1",
             "sender_name": "A", "images": []},
            {"id": "m-img", "text": "", "ts": now - 0.9, "sender": "u1",
             "sender_name": "A", "images": ["https://multimedia.nt.qq.com.cn/a.png"]},
        ]
        ev = _Event(raw=_Raw(elements=[{"content": "😶"}]))
        rep = p._mod_resolve_reported(ev)
        self.assertTrue(rep["resolved"])
        self.assertEqual(rep["text"], "😶")
        self.assertEqual(rep["images"], ["https://multimedia.nt.qq.com.cn/a.png"])
        self.assertIn("相邻缓存补齐", p.logger.text("info"))

    def test_nearby_images_respects_sender_and_window(self):
        p = _plugin()
        now = time.time()
        p._recent_group_msgs["g1"] = [
            {"id": "other", "text": "", "ts": now - 1, "sender": "u2",
             "sender_name": "x", "images": ["https://img/other.png"]},
            {"id": "far", "text": "", "ts": now - 30, "sender": "u1",
             "sender_name": "A", "images": ["https://img/far.png"]},
        ]
        self.assertEqual(p._nearby_images("g1", "u1", now - 1), [])


class ImageOnlyQuoteTest(unittest.TestCase):
    def test_image_only_quote_falls_back_to_latest_image(self):
        """引用纯图片消息：摘要没有正文，按最近图片消息兜底，不能直接判「定位不到」。"""
        p = _plugin()
        now = time.time()
        p._recent_group_msgs["g1"] = [
            {"id": "m-img", "text": "", "ts": now - 5, "sender": "u1",
             "sender_name": "A", "images": ["https://img/x.png"]},
        ]
        ev = _Event(raw=_Raw(elements=[{"content": ""}]))
        rep = p._mod_resolve_reported(ev)
        self.assertTrue(rep["resolved"])
        self.assertEqual(rep["images"], ["https://img/x.png"])
        self.assertIn("纯图片消息", p.logger.text("info"))

    def test_quote_attachments_are_normalized(self):
        """引用事件自带 attachments 时也要认，且补齐 https:// 前缀。"""
        p = _plugin()
        now = time.time()
        p._recent_group_msgs["g1"] = [
            {"id": "m-img", "text": "", "ts": now - 2, "sender": "u1",
             "sender_name": "A", "images": []},
        ]
        ev = _Event(raw=_Raw(elements=[{
            "content": "", "attachments": [{"content_type": "image/png", "url": "//gchat.qpic.cn/y.png"}],
        }]))
        rep = p._mod_resolve_reported(ev)
        self.assertEqual(rep["images"], ["https://gchat.qpic.cn/y.png"])


class ExtractImagesTest(unittest.TestCase):
    def test_raw_attachments_fallback(self):
        """组件里没有 Image 时，退回原始 attachments 取图。"""
        p = _plugin()
        raw = _Raw(elements=[], attachments=[{"content_type": "image/jpeg", "url": "//a/b.jpg"}],
                   message_type=0)
        ev = _Event(raw=raw, message=[])
        self.assertEqual(p._extract_images(ev), ["https://a/b.jpg"])

    def test_component_with_file_field(self):
        class Image:
            url = ""
            file = "https://img/c.webp"

        p = _plugin()
        ev = _Event(raw=_Raw(message_type=0), message=[Image()])
        self.assertEqual(p._extract_images(ev), ["https://img/c.webp"])

    def test_dedupes_and_caps(self):
        class Image:
            def __init__(self, u):
                self.url, self.file = u, ""

        p = _plugin()
        ev = _Event(raw=_Raw(message_type=0),
                    message=[Image("https://img/1.png")] + [Image("https://img/1.png")] * 3)
        self.assertEqual(p._extract_images(ev), ["https://img/1.png"])


if __name__ == "__main__":
    unittest.main()
