"""群规一行式写法解析自测（parse_rule_spec）。

起因：群里按帮助发文「/群管 群规 添加 侮辱 | 说明 | 2小时」时，
旧实现只认全角「｜」，半角「|」整句被当成规则名，说明和时长静默丢失。
本脚本直接对 main.py 里的真实函数做回归，覆盖三种分隔写法与异常输入。

运行：python3 tools/selftest_rule_spec.py
"""
import ast
import os
import sys
from typing import Optional

import re

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(PLUGIN, "main.py")

WANTED = {"parse_duration", "parse_rule_spec"}
ns = {"re": re, "Optional": Optional}
tree = ast.parse(open(SRC, encoding="utf-8").read())
# DURATION_RE / _RULE_SEP_RE / _CN_NUM 这些模块级常量先按行还原
for node in tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        if node.targets[0].id in ("DURATION_RE", "_RULE_SEP_RE", "_CN_NUM"):
            exec(compile(ast.Module([node], []), SRC, "exec"), ns)
    if isinstance(node, ast.FunctionDef) and node.name in WANTED:
        exec(compile(ast.Module([node], []), SRC, "exec"), ns)

parse_rule_spec = ns["parse_rule_spec"]

ok = lambda c, m: print(("  ✓ " if c else "  ✗ ") + m) or (c or sys.exit(1))

print("1) 三种分隔写法都要认")
cases = [
    ("全角竖线（帮助里的标准写法）", "刷屏｜连续刷无意义内容｜10分钟", ("刷屏", "连续刷无意义内容", 600)),
    ("半角竖线（手机/英文键盘，事故现场）", "侮辱 | 对他人发布侮辱性内容 | 2小时", ("侮辱", "对他人发布侮辱性内容", 7200)),
    ("竖线两边带空格 / 不带空格混排", "广告|发广告拉人|30分钟", ("广告", "发广告拉人", 1800)),
    ("纯空格三段", "刷屏 连续刷无意义内容 10分钟", ("刷屏", "连续刷无意义内容", 600)),
    ("只有名称", "恶意刷屏", ("恶意刷屏", "", 3600)),
    ("名称 + 说明，省略时长", "刷屏 | 连续刷无意义内容", ("刷屏", "连续刷无意义内容", 3600)),
    ("中文数字时长", "刷屏｜连续刷无意义内容｜一分钟", ("刷屏", "连续刷无意义内容", 60)),
    ("纯空格且说明含多词", "刷屏 连续 刷 无意义 内容 5分钟", ("刷屏", "连续 刷 无意义 内容", 300)),
]
for label, text, want in cases:
    name, desc, secs, err = parse_rule_spec(text)
    got = (name, desc, secs)
    ok(got == want and err is None, f"{label}：{text} -> {got}" + (f" (err={err})" if err else ""))

print("2) 异常输入不能静默建残缺规则")
for text in ("", "   "):
    name, desc, secs, err = parse_rule_spec(text)
    ok(err == "empty", f"空输入 -> err={err}")
long_name = "这是一个没有加任何分隔符而且长到离谱的规则名字应当被拦下来"
name, desc, secs, err = parse_rule_spec(long_name)
ok(err == "name_too_long", f"超长名称 -> err={err}（说明中间大概率漏了竖线）")

print("3) 时长解析与旧行为一致")
pd = ns["parse_duration"]
for text, want in [("10分钟", 600), ("2小时", 7200), ("1天", 86400), ("30秒", 30), ("一分钟", 60), ("半小时", 1800)]:
    ok(pd(text) == want, f"parse_duration({text}) = {pd(text)}")

print("\n全部通过 ✅")
