#!/usr/bin/env python3
"""变体检测自测（离线，不需要 AstrBot）。

跑法:
    python3 tools/selftest_variant.py

覆盖：谐音、形近、插入符号、正常文本不误报、白名单、词条字面命中不算变体。
"""

from __future__ import annotations

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from _plugin_import import load  # noqa: E402

variant = load("variant")

FAILED: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILED.append(msg)


def main() -> int:
    # 模拟词库导出的 (词, (类型, 词, 来源id, 来源名))
    words = [
        ("代刷单", ("ad", "代刷单", "test", "测试词库")),
        ("未端处理", ("other", "未端处理", "test", "测试词库")),
        ("代练", ("ad", "代练", "test", "测试词库")),
        ("末端", ("other", "末端", "test", "测试词库")),
        ("加微信", ("ad", "加微信", "test", "测试词库")),
        ("兼职", ("ad", "兼职", "test", "测试词库")),
    ]
    m = variant.VariantMatcher(BASE)
    m.set_words(words)
    st = m.stats()
    print(f"资源：拼音 {st['pinyin_chars']} 字 / 形近 {st['confusable_groups']} 组 / 索引 {st['indexed_words']} 词 "
          f"（{st['build_ms']}ms）")
    ok(st["pinyin_ready"], "拼音表加载成功")
    ok(st["confusable_ready"], "形近表加载成功")

    def kinds(text: str, allow=None):
        return [(h["method"], h["strength"]) for h in m.scan(text, allow=allow)]

    print("\n[1] 谐音（同音不同字）")
    r = m.scan("前排接袋刷单，需要的私我")
    ok(any(h["method"] == "pinyin" for h in r), f"三音节谐音「袋刷单」应命中 -> {r}")
    r2 = m.scan("我需要代刷单的服务")
    ok(not any(h["method"] == "pinyin" for h in r2), f"与词条字面一致时不算变体 -> {r2}")
    r3 = m.scan("有人能带我上个袋练吗")
    ok(not any(h["method"] == "pinyin" for h in r3),
       f"双音节词默认不做谐音匹配（同音词误报太高，需面板显式放宽）-> {r3}")
    r4 = m.scan("有人能带我上个袋练吗", min_tokens=2)
    ok(any(h["method"] == "pinyin" for h in r4), f"放宽到 2 音节后应命中 -> {r4}")

    print("\n[2] 形近（字形相近、读音不同）")
    r = m.scan("这个未端处理一下")
    ok(any(h["method"] == "confusable" for h in r), f"「未端」应命中 confusable 通道 -> {r}")

    print("\n[3] 插入符号/空格（归一化应还原）")
    r = m.scan("加·微·信 详聊")
    ok(any(h["method"] == "pinyin" for h in r), f"「加·微·信」应命中 -> {r}")

    print("\n[4] 正常文本不应误报")
    for t in ["今天天气不错，我们去吃饭吧", "这个方案我觉得可以再讨论一下", "周末一起去打球吗",
              "这本书我看完了，挺好看的", "记得把作业交到学习委员那里"]:
        r = m.scan(t)
        ok(not r, f"「{t[:12]}…」不应命中 -> {r}")

    print("\n[5] 白名单应放行")
    r = m.scan("推荐一个代刷单工具", allow={"代刷单"})
    ok(not r, f"白名单词命中应被跳过 -> {r}")

    print("\n[6] 模糊开关默认关闭（它是误报主要来源）")
    r6 = m.scan("今天天气不错，我们去吃饭吧", allow_fuzzy=True)
    ok(not r6, f"正常句子即使打开模糊也不应命中 -> {r6}")

    print("\n[7] 从内置正则里提取字面片段")
    frags = variant.literals_from_pattern(r"(?:刷单|返利|兼职日结|打字员|代刷|薅羊毛群)")
    ok("兼职日结" in frags and "薅羊毛群" in frags, f"应抠出字面片段 -> {frags}")
    frags2 = variant.literals_from_pattern(r"加\s*(?:我|微信|vx|VX|v信|威信|QQ|qq)")
    ok("微信" in frags2 and "威信" in frags2, f"应跳过单字、保留双字 -> {frags2}")

    print("\n[8] 超长/异常输入不应抛异常")
    try:
        m.scan("啊" * 5000)
        m.scan("")
        m.scan("🙂🙂🙂" * 100)
        ok(True, "空串/超长/纯表情均正常返回")
    except Exception as e:                                   # noqa: BLE001
        ok(False, f"不应抛异常，实得 {e!r}")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：")
        for f in FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
