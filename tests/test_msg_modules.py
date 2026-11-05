"""消息内置模块（@群员 / 群员头像）离线单元测试。

跑法（在插件目录下）：
    python3 -m unittest discover -s tests -v

完全离线：不发网络请求、不依赖 AstrBot 运行时，只校验占位符展开结果。
"""

import importlib
import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR.parent))

_m = importlib.import_module(_PLUGIN_DIR.name + ".main")
QQGroupManager = _m.QQGroupManager

APPID = "1903123456"
OID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"  # 32 位假 openid，形态同真实值


class FakeEvent:
    def __init__(self, sender_id=OID, sender_name="小明", pid="default"):
        self._sid = sender_id
        self._sname = sender_name
        self._pid = pid

    def get_platform_id(self):
        return self._pid

    def get_sender_id(self):
        return self._sid

    def get_sender_name(self):
        return self._sname


def make_plugin(appid=APPID):
    """不走 __init__，只借实例方法（避免依赖 AstrBot 运行时上下文）。"""

    class Stub(QQGroupManager):
        def _platform_appid(self, platform_id):
            return appid

    return Stub.__new__(Stub)


class AtTagTest(unittest.TestCase):
    def test_tag_format(self):
        self.assertEqual(
            make_plugin()._at_tag(OID),
            f'<qqbot-at-user id="{OID}" />',
        )

    def test_empty_openid(self):
        self.assertEqual(make_plugin()._at_tag(""), "")
        self.assertEqual(make_plugin()._at_tag(None), "")

    def test_strips_legacy_wrapper(self):
        self.assertEqual(
            make_plugin()._at_tag(f"<@{OID}>"),
            f'<qqbot-at-user id="{OID}" />',
        )


class ExpandModulesTest(unittest.TestCase):
    def test_no_placeholder_passthrough(self):
        ev = FakeEvent()
        self.assertEqual(make_plugin()._expand_msg_modules("普通文本", ev, "text"), "普通文本")

    def test_at_fallback_to_sender_qq(self):
        """_render 没传 at= 时，_reply 用消息发送者兜底。"""
        out = make_plugin()._expand_msg_modules("你好 {at}", FakeEvent(), "text")
        self.assertEqual(out, f'你好 <qqbot-at-user id="{OID}" />')

    def test_at_degrades_without_appid(self):
        """非 QQ 官方通道：退化文字 @昵称，绝不吐标签。"""
        out = make_plugin(appid="")._expand_msg_modules("你好 {at}", FakeEvent(), "text")
        self.assertEqual(out, "你好 @小明")

    def test_avatar_in_markdown(self):
        out = make_plugin()._expand_msg_modules("头像：{avatar}", FakeEvent(), "markdown")
        self.assertIn("![avatar #100px #100px]", out)
        self.assertIn(f"/qqapp/{APPID}/{OID}/100", out)

    def test_avatar_size_picks_hd_source(self):
        out = make_plugin()._expand_msg_modules("{avatar:200}", FakeEvent(), "markdown")
        self.assertIn("#200px #200px", out)
        self.assertIn("/640", out)  # 展示大于 100px 时取高清源

    def test_avatar_small_picks_small_source(self):
        """小图档只取 40 的源，别为表情大小的头像拉大图。"""
        out = make_plugin()._expand_msg_modules("{avatar:40}", FakeEvent(), "markdown")
        self.assertIn("#40px #40px", out)
        self.assertIn(f"/qqapp/{APPID}/{OID}/40", out)

    def test_avatar_tiny_display_upsizes_to_small_source(self):
        """展示尺寸比 40 还小时，仍取 40 的源（qlogo 最小公规格）。"""
        out = make_plugin()._expand_msg_modules("{avatar:24}", FakeEvent(), "markdown")
        self.assertIn("#24px #24px", out)
        self.assertIn(f"/qqapp/{APPID}/{OID}/40", out)

    def test_avatar_size_clamped(self):
        out = make_plugin()._expand_msg_modules("{avatar:9999}", FakeEvent(), "markdown")
        self.assertIn("#320px #320px", out)

    def test_avatar_dropped_in_plain_text(self):
        """纯文本消息塞不进 markdown 图片，静默去掉，不露语法。"""
        out = make_plugin()._expand_msg_modules("头:{avatar}", FakeEvent(), "text")
        self.assertEqual(out, "头:")

    def test_legacy_at_upgraded(self):
        out = make_plugin()._expand_msg_modules(f"已禁言 <@{OID}>", FakeEvent(), "text")
        self.assertEqual(out, f'已禁言 <qqbot-at-user id="{OID}" />')

    def test_legacy_at_kept_on_other_platform(self):
        out = make_plugin(appid="")._expand_msg_modules(f"<@{OID}>", FakeEvent(), "text")
        self.assertEqual(out, f"<@{OID}>")

    def test_would_you_kindly_not_mangle_markdown_links(self):
        src = "看这里 [🔗文档](https://bot.q.qq.com/wiki/)"
        self.assertEqual(make_plugin()._expand_msg_modules(src, FakeEvent(), "markdown"), src)


class TplTargetTest(unittest.TestCase):
    def test_tpl_text_carries_target(self):
        t = _m._TplText("x", "msg_mute_success", OID)
        self.assertEqual(t.tpl_key, "msg_mute_success")
        self.assertEqual(t.tpl_target, OID)

    def test_tpl_text_defaults(self):
        t = _m._TplText("x", "k")
        self.assertEqual(t.tpl_target, "")


class ModTplModuleGuardTest(unittest.TestCase):
    """moderation.render_tpl 会清空「未提供的 {变量}」，内置模块必须活下来。"""

    class _FakeMod:
        def __init__(self, tpls):
            self.tpls = tpls

        def get_templates(self, group):
            return self.tpls

    def _plugin(self, tpl):
        p = make_plugin()
        p.mod = self._FakeMod({"mod_test": tpl})
        return p

    def test_modules_survive_render_tpl(self):
        out = self._plugin("已处理 {user} {at}{avatar}{avatar:160}")._mod_tpl(
            "g", "mod_test", user="小明", at=OID
        )
        self.assertIn("{at}", out)
        self.assertIn("{avatar}", out)
        self.assertIn("{avatar:160}", out)
        self.assertIn("小明", out)

    def test_target_propagates(self):
        out = self._plugin("{at}")._mod_tpl("g", "mod_test", at=OID)
        self.assertEqual(getattr(out, "tpl_target", ""), OID)

    def test_unknown_var_still_cleared(self):
        """原有的「清掉未提供变量」行为不能被顺手破坏。"""
        out = self._plugin("依据《{rule_name}》处理")._mod_tpl("g", "mod_test")
        self.assertNotIn("{rule_name}", out)

    def test_end_to_end_at_tag(self):
        """话术模板 → _expand_msg_modules：{at} 最终变成官方标签。"""
        p = self._plugin("警告 {at}")
        rendered = p._mod_tpl("g", "mod_test", at=OID)
        final = p._expand_msg_modules(str(rendered), FakeEvent(), "text", rendered.tpl_target)
        self.assertEqual(final, f'警告 <qqbot-at-user id="{OID}" />')


class ReplyRoutingTest(unittest.IsolatedAsyncioTestCase):
    """_reply 的分支路由：图片消息正文可以空，纯文本/Markdown 全空则不发。"""

    class Ev(FakeEvent):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.sent = []

        async def send(self, chain):
            self.sent.append(chain)

    def _plugin(self, kind, url=""):
        p = make_plugin()
        p._tpl_formats = {"k": {"type": kind, "url": url}}
        return p

    async def test_image_with_empty_caption_still_sends(self):
        p = self._plugin("image", "https://example.com/a.png")
        ev = self.Ev()
        # {avatar} 在非 markdown 格式会被去掉，正文因此为空 —— 但图还得发
        await p._reply(ev, _m._TplText("{avatar}", "k", OID))
        self.assertEqual(len(ev.sent), 1)

    async def test_text_all_empty_skipped(self):
        p = self._plugin("text")
        ev = self.Ev()
        await p._reply(ev, _m._TplText("{avatar}", "k", OID))
        self.assertEqual(ev.sent, [])

    async def test_markdown_sends_at_tag(self):
        p = self._plugin("markdown")
        ev = self.Ev()
        await p._reply(ev, _m._TplText("警告 {at}", "k", OID))
        self.assertEqual(len(ev.sent), 1)
        self.assertIn("qqbot-at-user", ev.sent[0].chain[0].text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
