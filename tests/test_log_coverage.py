"""日志覆盖 lint：入口必须绑定上下文，面板接口必须绑定群。

- 运行时入口（群消息/按钮/成员事件/指令）用 `@_with_log_context` 绑定 [实例|群|成员]；
- 面板接口没有平台事件，至少从请求里绑定群；
- 核心审核链路仍要求显式 `_ctx()`（见 test_log_context_lint）。
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

ENTRY_FUNCS = {
    "on_group_message", "on_button_action",
    "_on_group_member_add", "_on_group_member_remove",
}


def _funcs():
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _decorator_names(node):
    names = []
    for d in node.decorator_list:
        if isinstance(d, ast.Name):
            names.append(d.id)
        elif isinstance(d, ast.Attribute):
            names.append(d.attr)
        elif isinstance(d, ast.Call):
            f = d.func
            names.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


class LogCoverageTest(unittest.TestCase):
    def test_runtime_entries_are_bound(self):
        missing = []
        for node in _funcs():
            if node.name in ENTRY_FUNCS and "_with_log_context" not in _decorator_names(node):
                missing.append(node.name)
        self.assertEqual(missing, [], f"入口缺少 @_with_log_context: {missing}")

    def test_commands_are_bound(self):
        missing = []
        for node in _funcs():
            names = _decorator_names(node)
            if "command" in names and "_with_log_context" not in names:
                missing.append(node.name)
        self.assertEqual(missing, [], f"指令入口缺少 @_with_log_context: {missing}")

    def test_panel_handlers_bind_group(self):
        missing = []
        for node in _funcs():
            if not node.name.startswith("web_"):
                continue
            seg = ast.get_source_segment(SRC, node) or ""
            if node.name == "web_config_bulk_save":
                continue      # 这里的 group 是配置分组（basic/moderation），不是 QQ 群
            if 'payload.get("group")' in seg or 'query.get("group"' in seg:
                if "_bind_log_group" not in seg:
                    missing.append(node.name)
        self.assertEqual(missing, [], f"面板接口解析了 group 但没绑定日志上下文: {missing}")


if __name__ == "__main__":
    unittest.main()
