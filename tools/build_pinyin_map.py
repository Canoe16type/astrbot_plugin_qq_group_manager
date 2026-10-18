#!/usr/bin/env python3
"""生成汉字 -> 拼音表（供变体检测的「谐音通道」用）。

为什么要在构建期生成：
    插件运行环境不保证能装第三方库（离线部署、受限容器），所以运行时不 import
    pypinyin，而是把它生成的表固化成 data/pinyin_map.json，运行时纯查表。
    本脚本不在插件加载时执行，只在本机手动跑一次（可复现）。

用法:
    python3 -m pip install --target /tmp/pylib pypinyin    # 或在任意有 pypinyin 的环境
    PYTHONPATH=/tmp/pylib python3 tools/build_pinyin_map.py

产物结构:
    {
      "generated_by": "tools/build_pinyin_map.py",
      "engine": "pypinyin x.y.z",
      "range": "U+4E00-U+9FFF（CJK 基本区）",
      "note": "值为不带声调的拼音；多音字用 | 分隔，第一个为主读音",
      "chars": {"一": "yi", "丁": "ding|zheng", ...}
    }
"""

from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "pinyin_map.json")

START, END = 0x4E00, 0x9FFF   # CJK 统一汉字 基本区


def main() -> int:
    try:
        import pypinyin
        from pypinyin import Style
    except ImportError:
        print("缺少 pypinyin：pip install pypinyin（或用 PYTHONPATH 指向临时安装目录）", file=sys.stderr)
        return 2

    chars: dict[str, str] = {}
    for cp in range(START, END + 1):
        ch = chr(cp)
        try:
            readings = pypinyin.pinyin(ch, style=Style.NORMAL, heteronym=True, errors="ignore")
        except Exception:
            continue
        if not readings or not readings[0]:
            continue
        seen: list[str] = []
        for r in readings[0]:
            r = str(r).strip().lower()
            if r and r not in seen:
                seen.append(r)
        if seen:
            chars[ch] = "|".join(seen)

    payload = {
        "generated_by": "tools/build_pinyin_map.py",
        "engine": f"pypinyin {getattr(pypinyin, '__version__', '?')}",
        "range": f"U+{START:04X}-U+{END:04X}（CJK 基本区）",
        "note": "值为不带声调的拼音；多音字用 | 分隔，第一个为主读音",
        "chars": chars,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)
    size = os.path.getsize(OUT)
    multi = sum(1 for v in chars.values() if "|" in v)
    print(f"已生成 {OUT}")
    print(f"  汉字 {len(chars)} 个（多音字 {multi} 个），文件 {size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
