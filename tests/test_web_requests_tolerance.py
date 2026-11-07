"""回归测试：/requests 汇总模式下，单个群的机器人平台失效不能拖垮整个接口。

历史 bug（1.4.0 未发版前修）：_refresh_requests 里 `self._get_api(pid)` 写在 try
之外，当某个群的 platform_id 已经没有活的平台实例（机器人下线 / 改名 / 平台被删）时
会抛 QQApiError，直接被 Hypercorn 兜成 500，前端「待处理的入群申请」整块
显示「加载失败: Internal server error」。

跑法（在插件目录下）：
    python3 -m unittest discover -s tests -v
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

# main.py 里有 `from .plugin_logger import ...` 相对导入，必须按包路径导入，
# 不能 `import main`（否则 ImportError: attempted relative import）。
import importlib  # noqa: E402

QQGroupManager = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.main"
).QQGroupManager
# 注意：一定要从「同一个包路径」取 QQApiError，否则 except 匹配的是另一个类对象，
# 测试会误判成「没被捕获」。
QQApiError = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.qq_api"
).QQApiError


class _Logger:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(("info", msg))

    def warning(self, msg):
        self.lines.append(("warning", msg))

    def error(self, msg):
        self.lines.append(("error", msg))


def _bare_manager():
    """只带 _refresh_requests 所需的属性，避免拉起整个插件实例。"""
    m = types.SimpleNamespace()
    m.logger = _Logger()
    m._pending = {}
    m._join_error_code = {}
    m._pending_list = lambda gid: list(m._pending.get(gid, []))
    return m


class RefreshRequestsToleranceTest(unittest.IsolatedAsyncioTestCase):
    async def test_platform_missing_falls_back_to_cache(self):
        m = _bare_manager()
        cached = [{"username": "老缓存", "member_openid": "u1"}]
        m._pending["g1"] = cached

        def _boom(pid):
            raise QQApiError(f"找不到平台实例: {pid}")

        m._get_api = _boom

        got = await QQGroupManager._refresh_requests(m, "g1", "已下线的机器人")
        self.assertEqual(got, cached, "平台失效时应退回本地缓存，而不是抛异常")
        self.assertTrue(any("拉取入群申请失败" in s for _, s in m.logger.lines))

    async def test_other_api_error_falls_back_to_cache(self):
        m = _bare_manager()
        m._pending["g1"] = [{"username": "x"}]

        class _BoomApi:
            async def get_join_request_list(self, gid, limit=20):
                raise QQApiError("接口炸了", code=500)

        m._get_api = lambda pid: _BoomApi()

        got = await QQGroupManager._refresh_requests(m, "g1", "pid")
        self.assertEqual(got, [{"username": "x"}])

    async def test_success_still_works(self):
        m = _bare_manager()
        items = [{"username": "新人", "member_openid": "u2"}]

        class _OkApi:
            async def get_join_request_list(self, gid, limit=20):
                return {"list": items}

        m._get_api = lambda pid: _OkApi()
        got = await QQGroupManager._refresh_requests(m, "g1", "pid")
        self.assertEqual(got, items)
        self.assertEqual(m._pending["g1"], items)


if __name__ == "__main__":
    unittest.main()
