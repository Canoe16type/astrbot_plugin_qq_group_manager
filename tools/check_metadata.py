#!/usr/bin/env python3
"""仓库元数据校验：metadata.yaml 必填项 + _conf_schema.json 可解析 + 版本号一致。

跑法:
    python3 tools/check_metadata.py

CI 里每次推送都跑；本地提 PR 前建议也跑一遍。
"""

from __future__ import annotations

import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED = ("name", "display_name", "desc", "version", "author", "entry", "support_platforms")
FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILED.append(msg)


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("  ⏭ 跳过：未安装 pyyaml（pip install pyyaml 后再跑）")
        return 0

    meta = yaml.safe_load(open(os.path.join(BASE, "metadata.yaml"), encoding="utf-8"))
    print("[metadata.yaml]")
    for key in REQUIRED:
        check(bool(meta.get(key)), f"必填字段 {key} 存在")
    check(meta.get("entry") == "main.py", "entry 指向 main.py")
    check(os.path.exists(os.path.join(BASE, meta.get("entry", ""))), "entry 文件真实存在")
    check("qq_official" in (meta.get("support_platforms") or []), "声明了 qq_official 平台")
    ver = str(meta.get("version", ""))
    check(bool(re.fullmatch(r"\d+\.\d+\.\d+", ver)), f"version 为 x.y.z 形式（当前 {ver!r}）")
    check(not ver.startswith("v"), "version 不带 v 前缀（市场按纯数字比对）")

    print("[_conf_schema.json]")
    try:
        schema = json.load(open(os.path.join(BASE, "_conf_schema.json"), encoding="utf-8"))
        check(isinstance(schema, dict) and bool(schema), "可解析为 JSON 对象")
    except Exception as e:  # noqa: BLE001
        check(False, f"JSON 解析失败：{e}")
        schema = {}

    print("[版本一致性]")
    if ver and os.path.exists(os.path.join(BASE, "CHANGELOG.md")):
        head = open(os.path.join(BASE, "CHANGELOG.md"), encoding="utf-8").read()
        check(bool(re.search(rf"^## {re.escape(ver)}(?:$|[\s（(])", head, re.M)), f"CHANGELOG 有 {ver} 条目")

    # README / GUIDE 的「实测 AstrBot x.y.z」最容易一处改了另一处忘，也卡住
    print("[实测 AstrBot 版本]")
    tested = {}
    for rel in ("README.md", os.path.join("docs", "GUIDE.md")):
        path = os.path.join(BASE, rel)
        if not os.path.exists(path):
            continue
        m = re.search(r"实测\s*(\d+\.\d+\.\d+)", open(path, encoding="utf-8").read())
        if m:
            tested[rel] = m.group(1)
    check(len(set(tested.values())) <= 1,
          "README 与 GUIDE 的「实测 AstrBot 版本」一致" + (f"（当前 {tested}）" if len(set(tested.values())) > 1 else f"（{next(iter(tested.values()), '未标注')}）"))
    if tested:
        try:
            import importlib.metadata as _md
            installed = _md.version("astrbot")
        except Exception:
            installed = ""
        if installed and installed not in set(tested.values()):
            # 只提醒不失败：CI 环境和作者本机未必同版本
            print(f"⚠️ 本机 AstrBot {installed} 与文档「实测」版本 {sorted(set(tested.values()))} 不同，确认是否要更新文档")

    # 文档里的 version 徽章最容易漂（1.2.0 那次就是漏了 GUIDE），一并卡住
    print("[文档版本徽章]")
    for rel in ("README.md", os.path.join("docs", "GUIDE.md")):
        path = os.path.join(BASE, rel)
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        m = re.search(r"img\.shields\.io/badge/version-([0-9][0-9.]*)-", text)
        check(bool(m), f"{rel} 有 version 徽章")
        if m:
            check(m.group(1) == ver, f"{rel} 徽章版本 = {ver}（当前 {m.group(1)}）")

    print()
    if FAILED:
        print(f"❌ 有 {len(FAILED)} 项未通过")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
