"""搬迁后签名回归：这些参数丢了会导致运行时报错，但单测不一定覆盖。"""

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.main import QQGroupManager  # noqa: E402
from plugins.astrbot_plugin_qq_group_manager.qgm.features.lexicon import upstream  # noqa: E402


class MovedSignatureTest(unittest.TestCase):
    def test_member_commands_keep_original_params(self):
        self.assertIn("group_openid", inspect.signature(QQGroupManager._cmd_member_sync).parameters)
        self.assertIn("event", inspect.signature(QQGroupManager._cmd_group_name).parameters)

    def test_upstream_fetch_signature(self):
        params = inspect.signature(upstream.fetch_upstream_lexicon).parameters
        for name in ("host", "url", "name", "category", "auto_enable", "mirror", "force_mirror"):
            self.assertIn(name, params)

    def test_static_helper_takes_settings_only(self):
        params = list(inspect.signature(QQGroupManager._mod_extra_variant_words).parameters)
        self.assertEqual(params, ["settings"])

    def test_image_url_helper_is_available(self):
        from plugins.astrbot_plugin_qq_group_manager.qgm.kernel import text

        self.assertTrue(text._looks_like_image_url("https://x/a.PNG"))
        self.assertFalse(text._looks_like_image_url("https://x/a.txt"))


if __name__ == "__main__":
    unittest.main()
