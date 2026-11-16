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
        self.assertLessEqual(lines, 8600, f"main.py 已 {lines} 行，超过预算；请拆分而不是继续堆")

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


if __name__ == "__main__":
    unittest.main()
