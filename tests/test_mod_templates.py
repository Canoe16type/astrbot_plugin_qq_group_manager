"""回归测试：审核话术（mod_*）的四层优先级必须真的生效。

历史 bug：面板「消息模板」页保存 mod_* 时写的是插件配置（templates.mod_hit），
而渲染函数 `_mod_tpl()` 只看 moderation 存储里的「群覆盖 / 全局话术」，
两层都空就直接回退内置默认 —— 于是「面板保存成功，群里话术一点没变」。

现在的优先级：本群覆盖 > 面板全局话术 > 插件配置 templates.* > 内置默认。
"""

import importlib
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

main = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
moderation = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.moderation"
)
QQGroupManager = main.QQGroupManager

GROUP = "G1"
KEY = "mod_hit"
DEFAULT = moderation.DEFAULT_TEMPLATES[KEY]


def _fake(mod_templates, config):
    plug = types.SimpleNamespace()
    plug.logger = types.SimpleNamespace(
        __getattr__=lambda name: (lambda *a, **k: None)
    )
    plug.mod = types.SimpleNamespace(
        templates=mod_templates,
        get_templates=lambda g: {
            **moderation.DEFAULT_TEMPLATES,
            **(mod_templates.get("global") or {}),
            **((mod_templates.get("groups") or {}).get(g) or {}),
        },
    )

    def _read(path, default=None, legacy_keys=None):
        return config.get(path[1], default)

    plug.config = {}  # 真插件一定有 config，这里给个占位让配置层生效
    plug._read_config_value = _read
    plug._MOD_MODULE_RE = QQGroupManager._MOD_MODULE_RE
    plug._mod_tpl = types.MethodType(QQGroupManager._mod_tpl, plug)
    return plug


class ModTplLayeringTest(unittest.TestCase):
    def test_config_value_is_used(self):
        plug = _fake({}, {KEY: "配置层话术"})
        self.assertEqual(str(plug._mod_tpl(GROUP, KEY)), "配置层话术")

    def test_global_store_beats_config(self):
        plug = _fake({"global": {KEY: "面板全局话术"}}, {KEY: "配置层话术"})
        self.assertEqual(str(plug._mod_tpl(GROUP, KEY)), "面板全局话术")

    def test_group_override_beats_everything(self):
        plug = _fake(
            {"global": {KEY: "面板全局话术"}, "groups": {GROUP: {KEY: "本群话术"}}},
            {KEY: "配置层话术"},
        )
        self.assertEqual(str(plug._mod_tpl(GROUP, KEY)), "本群话术")

    KW = {"basis": "B", "category": "C", "duration": "D"}

    def _expected_default(self):
        return moderation.render_tpl(DEFAULT, **self.KW)

    def test_falls_back_to_builtin_default(self):
        plug = _fake({}, {})
        self.assertEqual(str(plug._mod_tpl(GROUP, KEY, **self.KW)),
                         self._expected_default())

    def test_blank_config_falls_back_to_default(self):
        plug = _fake({}, {KEY: "   "})
        self.assertEqual(str(plug._mod_tpl(GROUP, KEY, **self.KW)),
                         self._expected_default())

    def test_at_placeholder_survives(self):
        plug = _fake({}, {KEY: "踢掉了 {at}"})
        out = str(plug._mod_tpl(GROUP, KEY, at="OPENID_1"))
        self.assertIn("{at}", out)  # 内置模块占位符不能被当未知变量清空


if __name__ == "__main__":
    unittest.main()
