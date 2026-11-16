"""`/群管 <子指令>` 矩阵解析测试。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.qgm.commands import parse, router  # noqa: E402


class CommandParseTest(unittest.TestCase):
    def test_group_matrix_mute_list(self):
        cmd, parts = parse.parse_command("/群管 禁言 名单", forced_cmd="/禁言", skip=1)
        self.assertEqual((cmd, parts), ("/禁言", ["/禁言", "名单"]))
        self.assertEqual(router.resolve_admin(cmd, parts)[0], "_cmd_list_mutes")

    def test_group_matrix_approval(self):
        cmd, parts = parse.parse_command("/群管 审批 通过 1", forced_cmd="/审批", skip=1)
        self.assertEqual(parts, ["/审批", "通过", "1"])
        self.assertEqual(router.resolve_admin(cmd, parts), ("_cmd_review", ["/通过", "1"]))

    def test_mentions_are_stripped(self):
        cmd, parts = parse.parse_command(
            "/群管 禁言 <@8D1DBFFB> 10分钟 刷屏", forced_cmd="/禁言", skip=1)
        self.assertEqual(parts, ["/禁言", "10分钟", "刷屏"])

    def test_group_alone(self):
        cmd, parts = parse.parse_command("/群管")
        self.assertEqual((cmd, parts), ("/群管", ["/群管"]))

    def test_plain_subcommand_is_not_a_public_entry(self):
        """用户侧只应有 /群管；单独 /禁言 不应被当成独立指令矩阵。"""
        cmd, parts = parse.parse_command("/禁言 名单")
        self.assertEqual(cmd, "/禁言")
        # 说明：g_mute 只注册在群管组下，_run_command 只从 /群管 进入；
        # 这里仅验证解析函数本身，不把它当成用户入口。
        self.assertEqual(parts, ["/禁言", "名单"])


if __name__ == "__main__":
    unittest.main()
