"""回归测试：面板/API 必须认得 Webhook 形态的 QQ 官方机器人。

背景：`_platform_list()` 以前写死 `meta.name != "qq_official"` 就跳过。
AstrBot 4.16 起官方机器人多了 `qq_official_webhook` 适配器（同一套开放平台
API、同样的 appid/secret，只是事件由平台推到本地 HTTP）。用 Webhook 模式的人
打开面板会看到「没有机器人」，概览/群管/审批全空，且后端 `_get_api()` 的
兜底也挑不到实例。

这里锁死：两种平台名都要被枚举出来，非 QQ 平台仍然要被过滤掉。
"""

import importlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

main = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
QQGroupManager = main.QQGroupManager


class _Logger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _platform(pid, name, appid="1234567890abcdef"):
    p = types.SimpleNamespace()
    p.meta = lambda: types.SimpleNamespace(id=pid, name=name)
    p.config = {"appid": appid, "secret": "s"}
    return p


def _fake_instance(platforms):
    plug = types.SimpleNamespace()
    plug.logger = _Logger()
    plug.mod = types.SimpleNamespace(get_bot_meta=lambda pid: {})
    plug.context = types.SimpleNamespace(
        platform_manager=types.SimpleNamespace(platform_insts=platforms)
    )
    plug._platform_list = types.MethodType(
        QQGroupManager._platform_list, plug
    )
    return plug


class PlatformFilterTest(unittest.TestCase):
    def test_webhook_platform_is_listed(self):
        plug = _fake_instance([
            _platform("bot-ws", "qq_official"),
            _platform("bot-wh", "qq_official_webhook", "abcdef0123456789"),
        ])
        ids = [p["id"] for p in plug._platform_list()]
        self.assertEqual(ids, ["bot-ws", "bot-wh"])

    def test_other_platforms_filtered(self):
        plug = _fake_instance([
            _platform("bot-ws", "qq_official"),
            _platform("tg", "telegram"),
            _platform("ob", "aiocqhttp"),
        ])
        self.assertEqual([p["id"] for p in plug._platform_list()], ["bot-ws"])

    def test_appid_masked(self):
        plug = _fake_instance([_platform("bot", "qq_official_webhook")])
        self.assertEqual(plug._platform_list()[0]["appid_masked"], "1234****cdef")


if __name__ == "__main__":
    unittest.main()
