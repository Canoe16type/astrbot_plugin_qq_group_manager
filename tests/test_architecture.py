"""架构守卫：分层规则、入口行数预算、关键工具唯一实现。"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "qgm"


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(("." * node.level) + (node.module or ""))
    return names


class ArchitectureGuardTest(unittest.TestCase):
    def test_kernel_has_no_business_dependencies(self):
        bad = []
        for f in (PKG / "kernel").glob("*.py"):
            for name in _imports(f):
                if any(x in name for x in ("features", "integrations", "main", "plugin_logger")):
                    bad.append(f"{f.name}: {name}")
        self.assertEqual(bad, [], f"kernel 不允许依赖业务层：{bad}")

    def test_qgm_never_imports_main(self):
        bad = [f.name for f in PKG.rglob("*.py")
               if any("main" in n.split(".") for n in _imports(f))]
        self.assertEqual(bad, [], f"qgm 内不允许 import main：{bad}")

    def test_atomic_write_has_single_implementation(self):
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("def _write_json_atomic(", main_src, "原子写实现应只在 qgm/kernel/storage.py")
        self.assertIn("def write_json_atomic(", (PKG / "kernel" / "storage.py").read_text(encoding="utf-8"))

    def test_main_line_budget(self):
        """棘轮：main.py 只许变短。重构时把这个数字往下调，不许往上抬。"""
        lines = len((ROOT / "main.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(lines, 4300, f"main.py 已 {lines} 行，超过预算；请拆分而不是继续堆")

    def test_legacy_shims_are_thin(self):
        """已迁移模块的旧路径只允许做别名转发，防止实现又搬回根目录。"""
        for name in ("qq_api.py", "plugin_logger.py", "lexicon.py", "variant.py", "button_kit.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertLessEqual(len(src.splitlines()), 20, f"{name} 已不再是薄 shim")
            self.assertIn("sys.modules", src)

    def test_report_handler_is_thin(self):
        """举报编排已迁到 service；入口方法超过 12 行说明逻辑又搬回来了。"""
        import ast as _ast
        tree = _ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name == "_cmd_report_violation":
                self.assertLessEqual(node.end_lineno - node.lineno + 1, 12)
                return
        self.fail("找不到 _cmd_report_violation")

    def test_member_handlers_are_thin(self):
        """成员事件已迁到 features/members；入口方法超过 12 行说明逻辑又搬回来了。"""
        import ast as _ast
        tree = _ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name in (
                    "_on_group_member_add", "_on_group_member_remove"):
                self.assertLessEqual(node.end_lineno - node.lineno + 1, 12, node.name)

    def test_review_handlers_are_thin(self):
        """判定层已迁到 features/audit/review；入口超过 12 行说明逻辑又搬回来了。"""
        import ast as _ast
        tree = _ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name in (
                    "_mod_review_llm", "_mod_review_fast"):
                self.assertLessEqual(node.end_lineno - node.lineno + 1, 12, node.name)

    def test_revoke_handlers_are_thin(self):
        """撤销逻辑已迁到 features/audit/revoke；入口超过 12 行说明逻辑又搬回来了。"""
        import ast as _ast
        tree = _ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name in (
                    "_cmd_revoke", "_revoke_verdict"):
                self.assertLessEqual(node.end_lineno - node.lineno + 1, 12, node.name)

    def test_moved_web_handlers_are_thin(self):
        """已迁出的 Web API 在 main 里只允许是薄壳。"""
        import ast as _ast
        moved = {
            "web_lexicons", "web_lexicon_detail", "web_lexicon_export",
            "web_lexicon_rename", "web_lexicon_delete", "web_lexicon_apply",
            "web_lexicon_toggle", "web_lexicon_fetch_upstream", "web_lexicon_upload",
            "web_lexicon_allow", "web_lexicon_test",
            "web_logs", "web_logs_stream", "web_logs_clear",
            "web_members", "web_members_stats", "web_members_update",
            "web_members_delete", "web_members_clear_left",
            "web_members_import", "web_members_sync",
            "web_config_save", "web_config_schema", "web_config_bulk_save",
            "web_mod_groups", "web_mod_group_rename", "web_mod_group_names_refresh",
            "web_mod_detail", "web_mod_rules_save", "web_mod_rules_transfer",
            "web_mod_settings_save", "web_mod_templates_save", "web_mod_verdicts_revoke",
            "web_mod_verdicts_prune", "web_mod_verdicts_export", "web_mod_verdicts",
            "web_mod_feedback", "web_mod_admins", "web_mod_admins_remove", "web_mod_todo",
            "web_mod_todo_handle", "web_mod_stats", "web_mod_test", "web_mod_verdicts_delete",
            "web_mod_verdicts_clear", "web_tpl_formats", "web_tpl_formats_save",
            "web_join_approval_list", "web_join_approval_save", "web_join_approval_toggle",
            "web_join_approval_delete", "web_join_approval_execute", "web_join_approval_whitelist",
            "web_mutes", "web_mute_history", "web_mute_history_clear", "web_mute_history_delete",
            "web_recalls", "web_recalls_clear", "web_recalls_delete",
            "web_status", "web_requests", "web_poll_now",
            "_cmd_mute", "_cmd_unmute", "_cmd_list_mutes",
            "_cmd_list_mute_history", "_cmd_review",
            "_cmd_status", "_cmd_selfcheck", "_cmd_report",
            "_cmd_admin_register", "_cmd_list_requests",
            "_cmd_recall", "_cmd_member", "_cmd_member_sync", "_cmd_presence",
            "_cmd_group_name", "_cmd_mod", "_cmd_rules",
            "_mod_execute", "_mod_variant_strike", "_send_group_proactive",
            "_maybe_auto_scan", "_on_group_join_request", "_notify_join_requests",
            "_welcome_member_on_join", "_mod_resolve_reported", "_extract_quote_from_raw",
            "_purge_group_data", "_handle_button_interaction", "_register_web_apis",
            "_load_recent_msgs", "_save_recent_msgs", "_record_recent_msg", "_match_recent_msg_id",
            "_record_member_seen", "_member_event_seen", "_ensure_member_event_injected",
            "_refresh_bot_name", "_resolve_member_name", "_member_ref", "_render_member_list",
            "_aggregate_member_stats", "_try_fetch_group_name", "_check_group_presence",
        }
        tree = _ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name in moved:
                self.assertLessEqual(node.end_lineno - node.lineno + 1, 12, node.name)

    def test_command_dispatch_is_table_driven(self):
        """指令分发应走路由表；_handle_command 出现大段 if/elif 说明又退回旧写法。"""
        src = (ROOT / "qgm" / "commands" / "runtime.py").read_text(encoding="utf-8")
        self.assertIn("command_router.resolve_admin", src)
        self.assertLessEqual(src.count("elif cmd =="), 2)


    def test_moved_modules_avoid_zero_arg_super(self):
        """零参 super() 依赖 __class__ 闭包，搬到模块函数会 RuntimeError。"""
        for rel in ("qgm/setup.py",):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("super()", src, f"{rel} 里不能用零参 super()")


if __name__ == "__main__":
    unittest.main()
