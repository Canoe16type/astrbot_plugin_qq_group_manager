"""前端分片守卫：单文件体积上限、脚本顺序、无高风险动态执行。"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "pages" / "dashboard" / "js"
HTML = (ROOT / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")


class PanelBundleTest(unittest.TestCase):
    def test_each_chunk_is_reasonably_small(self):
        # 单文件越大，越容易触发杀软「JS.Phishing」启发式误报
        for p in sorted(JS_DIR.glob("app*.js")):
            lines = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 1200, f"{p.name} 已有 {lines} 行，继续增长请再分片")

    def test_scripts_are_loaded_in_order(self):
        tags = re.findall(r'<script src="js/(app\d+\.js)" defer></script>', HTML)
        self.assertEqual(tags, [f"app{i:02d}.js" for i in range(1, 7)])
        for name in tags:
            self.assertTrue((JS_DIR / name).is_file(), name)

    def test_no_dynamic_code_execution(self):
        for p in sorted(JS_DIR.glob("app*.js")):
            src = p.read_text(encoding="utf-8")
            for bad in ("eval(", "new Function", "document.write", "atob("):
                self.assertNotIn(bad, src, f"{p.name} 含高风险调用 {bad}")


if __name__ == "__main__":
    unittest.main()
