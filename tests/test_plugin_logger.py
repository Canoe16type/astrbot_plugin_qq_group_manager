"""plugin_logger 的离线单测（只依赖标准库，不需要 AstrBot 运行环境）。

覆盖两处曾经踩过的坑：
1. 日志文件跨天要换名（长期运行的机器人不重启，否则今天写进昨天的文件，
   还会被留存策略当过期文件删掉）；
2. 后台落盘线程必须在属性初始化完成后才能启动。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

import plugin_logger  # noqa: E402


class TestDailyRollover(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = plugin_logger.PluginLogger(
            to_file=True, log_dir=self.tmp.name, keep_days=14,
            plugin_name="test_plugin",
        )

    def tearDown(self):
        self.logger.close()
        time.sleep(0.05)
        self.tmp.cleanup()

    def test_today_name_uses_given_date(self):
        name = self.logger._today_name(datetime(2020, 1, 2, 23, 59))
        self.assertEqual(name, "test_plugin_20200102.log")

    def test_roll_switches_to_today_file(self):
        yesterday = os.path.join(
            self.tmp.name,
            self.logger._today_name(datetime.now() - timedelta(days=1)),
        )
        self.logger.file_path = yesterday
        self.logger._roll_file_if_needed()
        self.assertNotEqual(self.logger.file_path, yesterday)
        self.assertTrue(
            self.logger.file_path.endswith(
                self.logger._today_name()
            ),
            f"跨天后应切到当天文件，实际 {self.logger.file_path}",
        )

    def test_write_and_flush_persists(self):
        self.logger.info("hello 日志")
        self.logger.flush()
        path = self.logger.file_path
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            self.assertIn("hello 日志", f.read())

    def test_cleanup_respects_keep_days(self):
        # 造一个 30 天前的空日志文件，cleanup 应删掉它
        old_name = self.logger._today_name(datetime.now() - timedelta(days=30))
        old_path = os.path.join(self.tmp.name, old_name)
        with open(old_path, "w", encoding="utf-8") as f:
            f.write("old\n")
        removed = self.logger.cleanup_old_files(force=True)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(os.path.exists(old_path))

    def test_cleanup_keeps_today_file(self):
        today_path = self.logger.file_path
        self.logger.info("keep me")
        self.logger.flush()
        self.logger.cleanup_old_files(force=True)
        self.assertTrue(os.path.exists(today_path), "当天日志文件不能被清理掉")


class TestInitOrder(unittest.TestCase):
    def test_flusher_starts_after_attrs(self):
        with tempfile.TemporaryDirectory() as d:
            lg = plugin_logger.PluginLogger(to_file=True, log_dir=d, plugin_name="t")
            try:
                self.assertIsNotNone(lg._flusher)
                self.assertTrue(lg._flusher.is_alive())
                # 属性齐全，flush 不应抛异常
                lg.info("x")
                lg.flush()
            finally:
                lg.close()


if __name__ == "__main__":
    unittest.main()
