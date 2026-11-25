"""处罚回执依据：群规条号与名称。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager import moderation  # noqa: E402
from plugins.astrbot_plugin_qq_group_manager.qgm.features.audit import basis  # noqa: E402


class _Mod:
    def get_rules(self, group):
        return [
            {"id": "r1", "name": "刷屏"},
            {"id": "rule_01", "name": "引人不适"},
        ]

    def get_dim_names(self, group):
        return {"semantic": "语义分析", "rule": "群规匹配"}


class _Host:
    mod = _Mod()


class BasisDetailTest(unittest.TestCase):
    def test_rule_index_and_name(self):
        out = basis.basis_detail(_Host(), "g", {"rule_id": "rule_01", "rule_name": "引人不适"})
        self.assertEqual(out, "群规第2条「引人不适」")

    def test_rule_without_id_uses_name(self):
        out = basis.basis_detail(_Host(), "g", {"rule_id": "", "rule_name": "引人不适"})
        self.assertEqual(out, "群规第2条「引人不适」")

    def test_semantic_falls_back_to_dimension(self):
        out = basis.basis_detail(_Host(), "g", {"rule_name": "", "basis": "semantic"})
        self.assertEqual(out, "语义分析")

    def test_default_template_uses_basis_detail(self):
        txt = moderation.DEFAULT_TEMPLATES["mod_hit"].format(
            basis_detail="群规第2条「引人不适」", category="其他违规", duration="10分钟")
        self.assertIn("依据", txt)
        self.assertIn("群规第2条「引人不适」", txt)
        self.assertIn("其他违规", txt)


if __name__ == "__main__":
    unittest.main()
