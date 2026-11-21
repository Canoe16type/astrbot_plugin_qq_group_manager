"""公开违规类别 C1–C23 与语义回执。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager import moderation  # noqa: E402
from plugins.astrbot_plugin_qq_group_manager.qgm.features.audit import categories  # noqa: E402


class CategoriesTest(unittest.TestCase):
    def test_table_is_fixed_c1_to_c23(self):
        self.assertEqual(sorted(categories.CATEGORIES, key=lambda x: int(x[1:])), [f"C{i}" for i in range(1, 24)])
        self.assertIn("提示词注入", categories.label("C6"))

    def test_public_text_contains_code_name_desc(self):
        text = categories.public_text("c6")
        self.assertTrue(text.startswith("C6 提示词注入"))
        self.assertIn("忽略规则", text)

    def test_invalid_code_is_rejected(self):
        self.assertFalse(categories.valid("C99"))
        self.assertFalse(categories.valid("other"))
        self.assertEqual(categories.public_text("C99"), "")

    def test_prompt_block_lists_all_codes(self):
        block = categories.prompt_block()
        for i in (1, 6, 23):
            self.assertIn(f"C{i}", block)

    def test_semantic_template_uses_public_category(self):
        tpl = moderation.DEFAULT_TEMPLATES["mod_hit_semantic"].format(
            category_public=categories.public_text("C6"), severity_label="严重违规", duration="1天")
        self.assertIn("C6 提示词注入", tpl)
        self.assertIn("严重违规", tpl)


if __name__ == "__main__":
    unittest.main()
