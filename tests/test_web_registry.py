"""Web API 注册契约：所有路由必须经 AstrBot register_web_api 统一注册。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.qgm.kernel.constants import WEB_API_PREFIX  # noqa: E402
from plugins.astrbot_plugin_qq_group_manager.qgm.web import registry  # noqa: E402


class _Ctx:
    def __init__(self):
        self.calls = []
    def register_web_api(self, path, handler, methods, desc):
        self.calls.append((path, tuple(methods), desc))


class WebRegistryTest(unittest.TestCase):
    def test_all_routes_registered_through_astrbot(self):
        ctx = _Ctx()
        class _Host:
            context = ctx
            def __getattr__(self, name):
                return lambda *a, **k: None
        host = _Host()
        registry._register_web_apis(host)
        self.assertGreater(len(ctx.calls), 50)
        keys = set()
        for path, methods, _ in ctx.calls:
            self.assertTrue(path.startswith(f"/{WEB_API_PREFIX}/"), path)
            for m in methods:
                self.assertIn(m, ("GET", "POST"))
                key = (path, m)
                self.assertNotIn(key, keys, f"duplicate route {key}")
                keys.add(key)


if __name__ == "__main__":
    unittest.main()
