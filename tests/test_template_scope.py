"""话术模板作用域：审核/举报话术只应出现在「群规审核 → 话术模板」，不要重复到设置页。"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager import moderation  # noqa: E402

JS = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "pages" / "dashboard" / "js").glob("app*.js")))
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
M = re.search(r"const TPL_FIELDS\s*=\s*\[(.*?)\];", JS, re.S)
MOD_KEYS = re.findall(r'\["([a-zA-Z0-9_]+)"', M.group(1))


class TemplateScopeTest(unittest.TestCase):
    def test_no_duplicate_moderation_templates_in_settings(self):
        dup = [k for k in MOD_KEYS if k in SCHEMA["templates"]["items"]]
        self.assertEqual(dup, [], f"以下审核/举报话术不应出现在设置页：{dup}")

    def test_moderation_templates_have_defaults(self):
        missing = [k for k in MOD_KEYS if k not in moderation.DEFAULT_TEMPLATES]
        self.assertEqual(missing, [], f"群规话术缺少内置默认值：{missing}")

    def test_count_unchanged_on_both_sides(self):
        self.assertEqual(len(MOD_KEYS), 30)
        self.assertGreaterEqual(len(SCHEMA["templates"]["items"]), 40)


if __name__ == "__main__":
    unittest.main()
