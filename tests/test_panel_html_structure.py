"""面板 HTML 结构 lint：div 必须闭合、所有页签 section 必须互为兄弟。

背景：删「词库详情卡片」时配对删错了一个 `</div>`，导致词典页容器未闭合，
后面的「记录/日志/设置」全被嵌进词典页——无论切哪个页签都一起显示。
单元测试碰不到 DOM，所以这里加一道结构检查。
"""

import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")


class PanelHtmlStructureTest(unittest.TestCase):
    def test_div_tags_balanced(self):
        opens = len(re.findall(r"<div\b", HTML))
        closes = len(re.findall(r"</div>", HTML))
        self.assertEqual(opens, closes, f"div 开闭不平衡：{opens} / {closes}")

    def test_all_panel_sections_are_siblings(self):
        depth = 0
        panel_depths = []
        for m in re.finditer(r"<div\b|</div>|<section\b[^>]*id=\"tab-([^\"]+)\"", HTML):
            tok = m.group(0)
            if tok.startswith("<div"):
                depth += 1
            elif tok == "</div>":
                depth -= 1
            else:
                panel_depths.append((m.group(1), depth))
        self.assertTrue(panel_depths)
        levels = {d for _, d in panel_depths}
        self.assertEqual(len(levels), 1, f"页签 section 深度不一致（会被嵌套）：{panel_depths}")

    def test_lexicon_detail_removed(self):
        self.assertNotIn("lex-detail-card", HTML)
        self.assertNotIn("lex-detail-body", HTML)

    def test_lexicon_table_has_five_columns(self):
        head = re.search(r"<table class=\"lex-tbl\">.*?</thead>", HTML, re.S).group(0)
        self.assertEqual(head.count("<th>"), 5)


class PanelJsBehaviorTest(unittest.TestCase):
    JS = (Path(__file__).resolve().parent.parent / "pages" / "dashboard" / "app.js").read_text(encoding="utf-8")

    def test_verdict_row_is_fully_clickable(self):
        self.assertIn("tr.onclick = () => showVerdictDetail(v);", self.JS)
        self.assertNotIn('data-vd="${v.id}"', self.JS)

    def test_row_buttons_stop_propagation(self):
        for marker in ('[data-fb]', '[data-ok]', '[data-dv]', '[data-rv]'):
            idx = self.JS.find(marker)
            self.assertGreater(idx, 0, marker)
            self.assertIn("stopPropagation", self.JS[idx:idx + 160], f"{marker} 未阻止冒泡")


if __name__ == "__main__":
    unittest.main()
