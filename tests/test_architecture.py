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
        self.assertLessEqual(lines, 9450, f"main.py 已 {lines} 行，超过预算；请拆分而不是继续堆")


if __name__ == "__main__":
    unittest.main()
