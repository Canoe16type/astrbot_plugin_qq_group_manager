"""回归测试：插件重载（terminate）时必须收干净后台任务。

背景：入群申请的「合并推送」是 `asyncio.create_task` + `sleep(2s)` 后才发消息。
原来 `terminate()` 只取消了轮询任务和日报任务，没管这批短命任务，
于是「2 秒内重载插件」会出现旧实例把批次发出去、新实例又发一次/或者
对着已关闭的 API 会话发包，群里就是重复通知 + 一堆 Unclosed session。

这里锁死两点：
1. terminate() 会取消 `_join_notify_tasks` 里还活着的任务并清空字典；
2. terminate() 在只有最小属性的实例上也能安全跑完（不炸 AttributeError）。
"""

import asyncio
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


def _fake_instance():
    p = types.SimpleNamespace()
    p.logger = _Logger()
    p._poll_task = None
    p._report_task = None
    p._join_notify_tasks = {}
    p._apis = {}
    p.members = types.SimpleNamespace(save=lambda: None)
    p.terminate = types.MethodType(QQGroupManager.terminate, p)
    return p


class TerminateCleanupTest(unittest.TestCase):
    def test_cancels_pending_join_notify_tasks(self):
        async def scenario():
            p = _fake_instance()
            task = asyncio.create_task(asyncio.sleep(3600))
            p._join_notify_tasks["g1"] = task
            await asyncio.sleep(0)          # 让任务真正跑起来
            await p.terminate()
            return task, dict(p._join_notify_tasks)

        task, leftover = asyncio.run(scenario())
        self.assertTrue(task.cancelled(), "待发的合并推送任务没有被取消")
        self.assertEqual(leftover, {}, "字典没清空，会在多群场景下越攒越多")

    def test_done_tasks_are_harmless(self):
        async def scenario():
            p = _fake_instance()
            done = asyncio.create_task(asyncio.sleep(0))
            await done
            p._join_notify_tasks["g1"] = done
            await p.terminate()             # 已完成的任务不该抛异常
            return dict(p._join_notify_tasks)

        self.assertEqual(asyncio.run(scenario()), {})


if __name__ == "__main__":
    unittest.main()
