"""配置单一来源：每组默认值必须和面板字段一一对应。

新增一个审核设置时，如果只改了 `DEFAULT_SETTINGS` 而忘了面板字段（或反过来），
这里会直接失败，避免"代码里有、面板上没有"或"面板能改、代码不读"。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager import moderation  # noqa: E402

JS = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "pages" / "dashboard" / "js").glob("app*.js")))
M = re.search(r"const S_KEYS\s*=\s*\[(.*?)\];", JS, re.S)
PANEL_KEYS = set(re.findall(r'"([a-zA-Z0-9_]+)"', M.group(1)))
DEFAULTS = set(moderation.ModerationStore.DEFAULT_SETTINGS)

# 这两个字段在面板有独立编辑器（关键词表格 / 白名单），不在 S_KEYS 里
CUSTOM_EDITORS = {"keywords", "whitelist"}


class ConfigSingleSourceTest(unittest.TestCase):
    def test_every_default_has_a_panel_field(self):
        missing = sorted(DEFAULTS - PANEL_KEYS - CUSTOM_EDITORS)
        self.assertEqual(missing, [], f"有默认值但面板改不了：{missing}")

    def test_every_panel_field_has_a_default(self):
        extra = sorted(PANEL_KEYS - DEFAULTS)
        self.assertEqual(extra, [], f"面板字段没有默认值（读取会拿到 None）：{extra}")

    def test_smart_penalty_family_is_complete(self):
        """智能分级相关开关必须成组存在，避免只加了一半。"""
        required = {
            "smart_penalty", "smart_tier_bump_at", "smart_max_multiplier",
            "smart_force_mute_priors", "warn_confidence_threshold", "rule_floor_high",
        }
        self.assertTrue(required <= DEFAULTS, f"缺少：{sorted(required - DEFAULTS)}")
        self.assertTrue(required <= PANEL_KEYS, f"面板缺少：{sorted(required - PANEL_KEYS)}")


if __name__ == "__main__":
    unittest.main()
