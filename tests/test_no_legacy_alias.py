"""1.6.0 最终版：旧指令别名已移除。

保留少量断言，防止有人把 _LEGACY_ALIASES / alias= 之类的兼容层又加回来。
"""

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

main_mod = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
QQGroupManager = main_mod.QQGroupManager
LEGACY = ("/解禁", "/查禁言", "/禁言历史", "/通过", "/拒绝", "/汇报", "/体检", "/自检", "/群名")


class NoLegacyAliasTest(unittest.TestCase):
    def test_admin_commands_only_new_names(self):
        for name in LEGACY:
            self.assertNotIn(name, main_mod.ADMIN_COMMANDS, f"{name} 应从 ADMIN_COMMANDS 移除")

    def test_alias_machinery_removed(self):
        self.assertFalse(hasattr(QQGroupManager, "_LEGACY_ALIASES"))
        self.assertFalse(hasattr(QQGroupManager, "_normalize_aliases"))
        self.assertFalse(hasattr(QQGroupManager, "_cmd_alias_of"))

    def test_source_has_no_alias_kwarg(self):
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("alias={", src)
        self.assertNotIn("_LEGACY_ALIASES", src)


if __name__ == "__main__":
    unittest.main()
