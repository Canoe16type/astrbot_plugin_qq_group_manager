"""静态卫生：未定义名/重复定义/危险调用/硬编码密钥。

需要 pyflakes（CI 会安装）；本地未安装时该用例跳过，不影响普通测试。
"""

import importlib.util
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|BEGIN [A-Z ]*PRIVATE KEY)")
DANGER = ("eval(", "exec(", "os.system(", "pickle.loads(", "yaml.load(")


@unittest.skipUnless(importlib.util.find_spec("pyflakes") is not None, "未安装 pyflakes")
class StaticHygieneTest(unittest.TestCase):
    def test_no_undefined_names_or_redefinitions(self):
        out = subprocess.run([sys.executable, "-m", "pyflakes", "main.py", "qgm"],
                             cwd=ROOT, capture_output=True, text=True).stdout
        bad = [l for l in out.splitlines() if "undefined name" in l or "redefinition" in l]
        self.assertEqual(bad, [], "\n".join(bad))

    def test_no_dangerous_calls_or_secrets(self):
        for p in [ROOT / "main.py", *(ROOT / "qgm").rglob("*.py")]:
            src = p.read_text(encoding="utf-8")
            for pat in DANGER:
                self.assertNotIn(pat, src, f"{p} 含 {pat}")
            self.assertIsNone(SECRET_RE.search(src), f"{p} 疑似硬编码密钥")

    def test_no_unmarked_silent_except(self):
        """静默 except 必须显式标注原因；未标注视为隐藏故障。"""
        import ast

        bad = []
        for path in [ROOT / "main.py", *(ROOT / "qgm").rglob("*.py")]:
            src = path.read_text(encoding="utf-8")
            lines = src.splitlines()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    ln = node.body[0].lineno
                    prev = lines[ln - 2] if 0 <= ln - 2 < len(lines) else ""
                    if "noqa: silent-cleanup" not in prev:
                        bad.append(f"{path}:{node.lineno}")
        self.assertEqual(bad, [], "未标注的静默 except：" + ", ".join(bad))


if __name__ == "__main__":
    unittest.main()
