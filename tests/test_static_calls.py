"""静态检查：方法重名覆盖 + self 调用参数个数不匹配。

为什么需要这个测试（1.4.0 血泪）：
  main.py 是 8000 行的单文件，两个 `_cmd_report` 并存时 Python 不报错，
  只是后者静默覆盖前者 —— `/群管 汇报` 因此直接 TypeError，跑任何功能测试
  都不会覆盖到这条路径，肉眼看 diff 也看不出来。同理，`self._reply("...")`
  少传 event 也不会在编译期暴露。

这两类错误都能用 AST 静态查出来，所以钉在测试里，改代码时自动兜住。
"""

from __future__ import annotations

import ast
import os
import unittest

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [
    "main.py",
    "moderation.py",
    "member_registry.py",
    "member_event.py",
    "lexicon.py",
    "variant.py",
    "qq_api.py",
    "plugin_logger.py",
    "button_kit.py",
]


def _parse(name: str) -> ast.Module:
    with open(os.path.join(PLUGIN_ROOT, name), encoding="utf-8") as f:
        return ast.parse(f.read(), filename=name)


def _walk_classes(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            yield node


class TestNoShadowedMethods(unittest.TestCase):
    def test_no_duplicate_method_names(self):
        """同类里同名方法只能有一个 —— 后定义的会静默覆盖前一个。"""
        problems = []
        for name in TARGETS:
            path = os.path.join(PLUGIN_ROOT, name)
            if not os.path.exists(path):
                continue
            tree = _parse(name)
            for cls in _walk_classes(tree):
                seen: dict[str, int] = {}
                for item in cls.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in seen:
                            problems.append(
                                f"{name}: {cls.name}.{item.name} "
                                f"@{seen[item.name]} 与 @{item.lineno} 重名"
                            )
                        seen[item.name] = item.lineno
        self.assertEqual(problems, [], "发现重名方法（后者会覆盖前者）：\n" + "\n".join(problems))

    def test_no_duplicate_module_level_functions(self):
        problems = []
        for name in TARGETS:
            path = os.path.join(PLUGIN_ROOT, name)
            if not os.path.exists(path):
                continue
            tree = _parse(name)
            seen: dict[str, int] = {}
            for item in tree.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if item.name in seen:
                        problems.append(
                            f"{name}: {item.name} @{seen[item.name]} 与 @{item.lineno} 重名"
                        )
                    seen[item.name] = item.lineno
        self.assertEqual(problems, [], "发现模块级重名定义：\n" + "\n".join(problems))


class TestSelfCallArity(unittest.TestCase):
    """self.xxx(...) 的实参必须能填满定义的必填参数（含关键字传参）。"""

    def _signatures(self, tree: ast.Module) -> dict[str, dict]:
        table: dict[str, dict] = {}
        for cls in _walk_classes(tree):
            for item in cls.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                args = item.args
                pos = [a.arg for a in args.posonlyargs + args.args]
                if pos and pos[0] in ("self", "cls"):
                    pos = pos[1:]
                required_pos = pos[: len(pos) - len(args.defaults)]
                required_kwonly = [
                    a.arg
                    for a, d in zip(args.kwonlyargs, args.kw_defaults)
                    if d is None
                ]
                table[item.name] = {
                    "required_pos": required_pos,
                    "positional": pos,
                    "required_kwonly": required_kwonly,
                    "kwonly": [a.arg for a in args.kwonlyargs],
                    "vararg": bool(args.vararg),
                    "kwarg": bool(args.kwarg),
                    "line": item.lineno,
                }
        return table

    def test_self_calls_match_signature(self):
        problems = []
        for name in TARGETS:
            path = os.path.join(PLUGIN_ROOT, name)
            if not os.path.exists(path):
                continue
            tree = _parse(name)
            table = self._signatures(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                    continue
                if func.value.id not in ("self", "cls"):
                    continue
                sig = table.get(func.attr)
                if sig is None:
                    continue
                if any(isinstance(a, ast.Starred) for a in node.args):
                    continue
                if any(k.arg is None for k in node.keywords):
                    continue
                npos = len(node.args)
                kw_names = {k.arg for k in node.keywords}
                # 位置参数不能超过定义声明的上限
                if not sig["vararg"] and npos > len(sig["positional"]):
                    problems.append(
                        f"{name}:{node.lineno} self.{func.attr}({npos} 个位置参数)，"
                        f"但定义（@{sig['line']}）只接受 {len(sig['positional'])} 个"
                    )
                    continue
                # 必填参数必须被填满（位置或关键字）
                for idx, pname in enumerate(sig["required_pos"]):
                    if idx >= npos and pname not in kw_names:
                        problems.append(
                            f"{name}:{node.lineno} self.{func.attr}(...) 缺少必填参数 "
                            f"{pname!r}（定义 @{sig['line']}）"
                        )
                for pname in sig["required_kwonly"]:
                    if pname not in kw_names:
                        problems.append(
                            f"{name}:{node.lineno} self.{func.attr}(...) 缺少必填关键字参数 "
                            f"{pname!r}（定义 @{sig['line']}）"
                        )
                # 不能传定义里不存在的关键字
                if not sig["kwarg"]:
                    allowed = set(sig["positional"]) | set(sig["kwonly"])
                    unknown = kw_names - allowed
                    if unknown:
                        problems.append(
                            f"{name}:{node.lineno} self.{func.attr}(...) 收到未定义的关键字参数 "
                            f"{sorted(unknown)}（定义 @{sig['line']}）"
                        )
        self.assertEqual(problems, [], "发现参数不匹配的调用：\n" + "\n".join(problems))


class TestBotpyPatchGuarded(unittest.TestCase):
    """patch botpy 私有属性前必须探测存在性（官方审查的技术债要求）。"""

    def test_patch_checks_attributes(self):
        with open(os.path.join(PLUGIN_ROOT, "member_event.py"), encoding="utf-8") as f:
            src = f.read()
        for attr in ("_session_list", "_active_websockets", "_connection"):
            if attr in src:
                self.assertIn(
                    "getattr", src,
                    "member_event.py 里 patch 了 botpy 私有属性，必须用 getattr 探测后再 patch",
                )
                break


if __name__ == "__main__":
    unittest.main()
