"""回归测试：通用禁止项（common_ban）内置默认值。

默认值是一份 L4–L0 分级类别参考，必须：
- 与插件实际输出契约对齐（category 枚举、severity low|medium|high、matched_rule_id）；
- 不出现插件不支持的模板占位符（{{GROUP_RULES}} / {{GROUP_TOPIC}}）；
- 不承诺插件做不到的动作（官方接口没有踢人）；
- 能真的进到系统提示词里（build_system_prompt 注入）。

跑法（插件目录下）：
    python3 -m unittest tests.test_common_ban_default -v
"""

import importlib
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

moderation = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.moderation")

SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
TEXT = SCHEMA["moderation"]["items"]["common_ban"]["default"]


class CommonBanDefaultTest(unittest.TestCase):
    def test_default_is_not_empty_and_has_levels(self):
        self.assertGreater(len(TEXT), 800)
        for level in ("L4", "L3", "L2", "L1", "L0"):
            self.assertIn(level, TEXT)

    def test_categories_match_plugin_enum(self):
        allowed = set(moderation.DEFAULT_CAT_NAMES.keys())
        used = set(re.findall(r"（([a-z]+)）", TEXT))
        self.assertTrue(used, "应标注每个类别的映射 key")
        self.assertTrue(used <= allowed, f"出现了插件不认的 category: {used - allowed}")

    def test_severity_words_match_contract(self):
        for sev in ("low", "medium", "high"):
            self.assertIn(f"severity={sev}", TEXT)

    def test_no_unsupported_placeholders(self):
        self.assertNotIn("{{", TEXT)
        self.assertNotIn("GROUP_RULES", TEXT)
        self.assertNotIn("GROUP_TOPIC", TEXT)

    def test_no_promise_of_kick(self):
        """官方接口没有踢人能力，默认文案不能写「踢出」。"""
        self.assertNotIn("踢出", TEXT)
        self.assertNotIn("踢人", TEXT)

    def test_injected_into_system_prompt(self):
        prompt = moderation.build_system_prompt(common_ban=TEXT)
        self.assertIn("通用禁止项", prompt)
        self.assertIn("C1 ", prompt)
        # 分类表必须出现在通用禁止项之前（文案里引用了「上面分类表」）
        self.assertLess(prompt.index("category 必须取下列 key"), prompt.index("通用禁止项"))


if __name__ == "__main__":
    unittest.main()
