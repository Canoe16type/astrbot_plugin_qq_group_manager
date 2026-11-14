"""回归测试：词库开关必须真的影响判定（变体索引随签名重建）。

线上问题：后台停用词库后，判定仍能命中——因为变体索引只在首次构建，
`set_words()` 也每次都调用，但索引内容没跟启用集合更新。
"""

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

main_mod = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
QQGroupManager = main_mod.QQGroupManager


class _Logger:
    def _noop(self, *a, **k):
        pass

    debug = info = warning = error = _noop


class _Variant:
    def __init__(self):
        self.calls = 0
        self.words = []

    def set_words(self, words):
        self.calls += 1
        self.words = list(words)


class _Lexicons:
    def __init__(self):
        self.sig = "a"
        self.words = [("测试词", ("abuse", "测试词", "x", "库"))]

    def signature(self):
        return self.sig

    def literal_words(self):
        return list(self.words)


def _plugin():
    p = object.__new__(QQGroupManager)
    p.logger = _Logger()
    p.lexicons = _Lexicons()
    p.variant = _Variant()
    p._variant_words = None
    p._variant_sig = ""
    return p


class VariantSyncTest(unittest.TestCase):
    def test_build_once_then_reuse(self):
        p = _plugin()
        p._variant_sync()
        self.assertEqual(p.variant.calls, 1)
        p._variant_sync()
        self.assertEqual(p.variant.calls, 1, "签名没变不应重建")

    def test_rebuild_when_lexicon_toggled(self):
        p = _plugin()
        p._variant_sync()
        p.lexicons.sig = "b"          # 模拟停用/启用词库后签名变化
        p._variant_sync()
        self.assertEqual(p.variant.calls, 2, "词库变了必须重建，否则停用不生效")

    def test_force_rebuild(self):
        p = _plugin()
        p._variant_sync()
        p._variant_sync(force=True)
        self.assertEqual(p.variant.calls, 2)


class ReportAckTest(unittest.TestCase):
    def test_ack_enabled_by_default(self):
        moderation = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.moderation")
        self.assertTrue(moderation.ModerationStore.DEFAULT_SETTINGS.get("report_ack_enabled", False))


class RevokeTemplateTest(unittest.TestCase):
    """撤销致歉必须走 markdown（否则 <qqbot-at-user> 不会渲染），且含独立致歉句。"""

    def test_revoke_default_is_markdown(self):
        p = object.__new__(QQGroupManager)
        p._tpl_formats = {}
        self.assertEqual(p._tpl_format("msg_mod_revoke"), "markdown")

    def test_default_has_apology_sentence(self):
        moderation = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.moderation")
        text = moderation.DEFAULT_TEMPLATES["msg_mod_revoke"]
        self.assertIn("非常抱歉", text)
        self.assertIn("LLM 审核", text)

    def test_legacy_default_is_migrated(self):
        old = QQGroupManager._LEGACY_TEMPLATE_DEFAULTS["msg_mod_revoke"]
        saved = {}

        class _Cfg(dict):
            def save_config(self):
                saved["ok"] = True

        p = object.__new__(QQGroupManager)
        p.logger = _Logger()
        p.config = _Cfg({"templates": {"msg_mod_revoke": old}})
        p._migrate_template_defaults()
        self.assertIn("非常抱歉", p.config["templates"]["msg_mod_revoke"])
        self.assertTrue(saved.get("ok"))

    def test_user_customized_template_untouched(self):
        class _Cfg(dict):
            def save_config(self):
                pass

        p = object.__new__(QQGroupManager)
        p.logger = _Logger()
        p.config = _Cfg({"templates": {"msg_mod_revoke": "我自己改的 {vid}"}})
        p._migrate_template_defaults()
        self.assertEqual(p.config["templates"]["msg_mod_revoke"], "我自己改的 {vid}")


if __name__ == "__main__":
    unittest.main()
