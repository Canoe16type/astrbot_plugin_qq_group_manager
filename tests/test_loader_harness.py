"""Loader 冒烟：按 AstrBot 的方式真实构造插件。

单测大多用 `object.__new__` 造插件，测不到构造签名、super()、注册表这些
"只有真实加载才会踩"的问题（1.7.0 重构期间已踩两次）。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from astrbot.api.star import Star  # noqa: E402
from plugins.astrbot_plugin_qq_group_manager.main import QQGroupManager  # noqa: E402


class _Ctx:
    def __init__(self):
        self.apis = []

    def register_web_api(self, path, handler, methods=None, description=""):
        self.apis.append((path, tuple(methods or ()), handler))


class _Config:
    """最小配置对象：只实现插件用到的 get()。"""

    def get(self, key, default=None):
        return default


class _Harness(QQGroupManager):
    """把数据目录指到临时目录，避免污染真实 plugin_data。"""

    def _plugin_data_dir(self):
        return self._tmpdir


class LoaderHarnessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = _Ctx()
        _Harness._tmpdir = self._tmp.name      # 构造期间就要用到
        self.plugin = _Harness(context=self.ctx, config=_Config())

    def test_is_star_and_signature(self):
        self.assertTrue(issubclass(QQGroupManager, Star))
        import inspect
        params = inspect.signature(QQGroupManager.__init__).parameters
        self.assertIn("context", params)
        self.assertIn("config", params)

    def test_web_apis_registered_once(self):
        paths = [p for p, _, _ in self.ctx.apis]
        self.assertGreaterEqual(len(paths), 60)
        self.assertEqual(len(paths), len(set(paths)), "存在重复注册的 Web API")

    def test_registered_handlers_are_bound(self):
        for path, methods, handler in self.ctx.apis:
            self.assertTrue(path.startswith("/"))
            self.assertTrue(callable(handler), path)
            self.assertTrue(methods)

    def test_terminate_is_safe(self):
        asyncio.run(self.plugin.terminate())

    def test_variant_assets_load_from_plugin_root(self):
        """搬迁后 __file__ 会指向 qgm/；变体/词库资源必须仍从插件根加载。"""
        self.assertEqual(self.plugin.variant.dir, str(ROOT))
        self.plugin.variant._load_assets()
        self.assertTrue(getattr(self.plugin.variant, "_py_map", {}), "拼音表未加载")
        self.assertTrue(getattr(self.plugin.variant, "_cf_map", {}), "形近表未加载")

    def test_resources_are_loaded_from_plugin_root(self):
        """构造完成后，词库/模板/变体资源都应指向插件根并可用。"""
        from plugins.astrbot_plugin_qq_group_manager.qgm.kernel.constants import plugin_root

        self.assertEqual(plugin_root(), ROOT)
        self.assertTrue((ROOT / "data" / "pinyin_map.json").is_file())
        self.assertTrue((ROOT / "data" / "variant_confusables.json").is_file())
        self.assertGreaterEqual(len(self.plugin.lexicons.scan()), 2, "内置词库未加载")
        self.plugin.variant._load_assets()
        self.assertTrue(getattr(self.plugin.variant, "_py_map", {}), "拼音表未加载")
        self.assertTrue(getattr(self.plugin.variant, "_cf_map", {}), "形近表未加载")


if __name__ == "__main__":
    unittest.main()
