"""日志规范 lint：核心链路里的 logger 调用必须带上下文前缀。

目的：防止新写的日志又退化成「只有一句 group=xxx」或完全看不出是哪个实例/群。
`_ctx()` 产出 `[实例|群|成员|记录]`；`traceback.format_exc()` 跟随前一条 ERROR，允许不带。
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "main.py").read_text(encoding="utf-8")

CORE_FUNCS = {
    "_mod_review_llm", "_mod_review_fast", "_mod_execute",
    "_revoke_verdict", "_cmd_revoke",
    "_on_group_member_remove", "_on_group_member_add",
    "_mod_resolve_reported",
}
LOG_LEVELS = {"debug", "info", "warning", "error", "exception"}


class LogContextLintTest(unittest.TestCase):
    def test_core_functions_log_with_context(self):
        tree = ast.parse(SRC)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in CORE_FUNCS:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                if not (isinstance(fn, ast.Attribute) and fn.attr in LOG_LEVELS):
                    continue
                owner = fn.value
                if not (isinstance(owner, ast.Attribute) and owner.attr == "logger"):
                    continue
                seg = ast.get_source_segment(SRC, call) or ""
                if "format_exc" in seg:
                    continue          # 堆栈跟随上一条已带上下文的 ERROR
                if "_ctx(" not in seg and "{ctx}" not in seg and "ctx}" not in seg:
                    offenders.append(f"{node.name}:{call.lineno}: {seg[:110]}")
        self.assertEqual(offenders, [], "以下日志缺少 _ctx() 上下文：\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
