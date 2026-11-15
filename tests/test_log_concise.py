"""简洁日志模式：INFO 只留「后果类」，过程日志压到 DEBUG。"""

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

pl = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.plugin_logger")


class ConciseModeTest(unittest.TestCase):
    def _logger(self, concise):
        lg = pl.PluginLogger(max_entries=50, level="INFO", to_file=False)
        lg.concise = concise
        return lg

    def test_process_logs_dropped_in_concise(self):
        lg = self._logger(True)
        lg.info("[实例 default|群 123]收到群消息")
        lg.info("[实例 default|群 123][审核] 缓存命中")
        self.assertEqual(len(lg.buffer), 0, "过程日志不应进 INFO")

    def test_outcome_logs_kept_in_concise(self):
        lg = self._logger(True)
        lg.info("[实例 default|群 123][审核] 决策 action=warn")
        lg.info("[实例 default|群 123|记录 #9][审核] 已记录 动作=warn")
        lg.info("[审核] 禁言失败: timeout")
        self.assertEqual(len(lg.buffer), 3, "决策/记录/失败类必须保留")

    def test_warning_error_always_kept(self):
        lg = self._logger(True)
        lg.warning("[实例 default|群 123] 普通警告")
        lg.error("[实例 default|群 123] 出错了")
        self.assertEqual(len(lg.buffer), 2)

    def test_non_concise_keeps_everything(self):
        lg = self._logger(False)
        lg.info("收到群消息")
        self.assertEqual(len(lg.buffer), 1)


if __name__ == "__main__":
    unittest.main()
