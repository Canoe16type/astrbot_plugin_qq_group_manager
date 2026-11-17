"""指令路由表测试：名称解析与子指令别名。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.qgm.commands import router  # noqa: E402


class CommandRouterTest(unittest.TestCase):
    def test_simple_commands(self):
        self.assertEqual(router.resolve_admin("/成员", ["/成员"]), ("_cmd_member", ["/成员"]))
        self.assertEqual(router.resolve_admin("/撤销", ["/撤销", "@a"]),
                         ("_cmd_revoke", ["/撤销", "@a"]))
        self.assertIsNone(router.resolve_admin("/不存在", ["/不存在"]))

    def test_mute_subcommands(self):
        self.assertEqual(router.resolve_admin("/禁言", ["/禁言", "解除", "@a"]),
                         ("_cmd_unmute", ["/禁言", "@a"]))
        self.assertEqual(router.resolve_admin("/禁言", ["/禁言", "名单"]),
                         ("_cmd_list_mutes", ["/禁言", "名单"]))
        self.assertEqual(router.resolve_admin("/禁言", ["/禁言", "历史", "@a"]),
                         ("_cmd_list_mute_history", ["/禁言", "@a"]))
        self.assertEqual(router.resolve_admin("/禁言", ["/禁言", "@a", "刷屏"]),
                         ("_cmd_mute", ["/禁言", "@a", "刷屏"]))

    def test_approval_subcommands(self):
        self.assertEqual(router.resolve_admin("/审批", ["/审批", "通过", "1"]),
                         ("_cmd_review", ["/通过", "1"]))
        self.assertEqual(router.resolve_admin("/审批", ["/审批", "拒绝", "1"]),
                         ("_cmd_review", ["/拒绝", "1"]))
        self.assertEqual(router.resolve_admin("/审批", ["/审批"]),
                         ("_cmd_list_requests", ["/审批"]))

    def test_routes_have_compatible_signatures(self):
        """路由统一以 (event, parts) 调用；handler 必须能接住，防止再出错位。"""
        import inspect
        from plugins.astrbot_plugin_qq_group_manager.main import QQGroupManager

        cases = [
            ("/审核", ["/审核"]), ("/禁言", ["/禁言", "@a", "10分钟"]),
            ("/禁言", ["/禁言", "解除", "@a"]), ("/禁言", ["/禁言", "名单"]),
            ("/禁言", ["/禁言", "历史", "@a"]), ("/审批", ["/审批"]),
            ("/审批", ["/审批", "通过", "1"]), ("/审批", ["/审批", "拒绝", "1"]),
            ("/状态", ["/状态"]), ("/撤销", ["/撤销", "3"]), ("/撤回", ["/撤回"]),
            ("/管理登记", ["/管理登记"]), ("/成员", ["/成员"]),
            ("/踢人", ["/踢人", "@a", "理由"]),
        ]
        for cmd, parts in cases:
            route = router.resolve_admin(cmd, parts)
            self.assertIsNotNone(route, cmd)
            name, args = route
            fn = getattr(QQGroupManager, name, None)
            self.assertIsNotNone(fn, name)
            params = [p for p in inspect.signature(fn).parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            self.assertGreaterEqual(len(params), 3, f"{name} 不接受 (event, parts)")
            self.assertLessEqual(len(params), 3, f"{name} 参数过多，路由无法统一调用")


if __name__ == "__main__":
    unittest.main()
