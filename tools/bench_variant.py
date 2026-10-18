#!/usr/bin/env python3
"""变体检测性能与误报实测（用真实内置词库跑，离线）。

跑法:
    python3 tools/bench_variant.py

目的：
1. 量一下 4.4 万条词库建索引要多久、扫描一条消息要多久（审核链路对延迟敏感）；
2. 拿一批**正常群聊句子**跑一遍，看有没有误报（变体通道默认只记录/只撤回，但误报率仍要可观测）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(BASE))

from _plugin_import import load  # noqa: E402

lexicon_lib = load("lexicon")
variant = load("variant")

NORMAL = [
    "今天天气不错，我们去吃饭吧",
    "这个方案我觉得可以再讨论一下，先把数据补上",
    "周末一起去打球吗？我约了老张他们",
    "这本书我看完了，挺好看的，推荐给你",
    "记得把作业交到学习委员那里，明天截止",
    "刚看完一部动画，作画质量真不错",
    "我把代码重构了一版，性能提升挺明显的",
    "新买的键盘手感很好，敲起来很舒服",
    "晚上八点开会，记得准时参加",
    "这家店的面条分量很足，价格也实惠",
    "他今天心情不太好，别去打扰他",
    "信号有点差，我这边听不太清楚",
    "昨天的比赛太精彩了，最后一分钟绝杀",
    "我在整理这个月的账单，稍等一下",
    "天气转凉了，记得加件外套",
    "这个公式我推导了三遍才对",
    "服务器又重启了一次，应该能稳定了",
    "文件夹里那份文档更新过了，你看最新的",
    "帮我把这张表转成 PDF 吧",
    "周末去爬山吗？天气预报说是晴天",
    "帮我看一下这段报错是什么意思",
    "冰箱里的牛奶记得喝掉，别放过期了",
    "地铁上人太多了，站了半小时",
    "这个问题我们下周再约时间聊",
    "我刚把论文的参考文献整理完",
    "手机电量只剩百分之十了",
    "楼下新开了一家早餐店，豆浆很不错",
    "他打球的时候扭到脚了，休息几天",
    "把空调温度调高一点，有点冷",
    "明天的机票是早上七点的",
    "这道题我算了半天也没算出来",
    "你看一下邮件，有份合同要确认",
    "小区门口在修路，绕道走吧",
    "我妈让我周末回家吃饭",
    "这周末有个展会，要不要一起去",
    "水杯里泡点茶叶挺香的",
    "路由器重启一下应该就好了",
    "他们把会议室改到三楼了",
    "这批货明天下午到仓库",
    "我在图书馆借了两本书",
    "电视剧更新到第八集了",
    "记得给我的车加满油",
    "这个月的体检报告出来了",
    "他把旧电脑卖给同学了",
]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="variant_bench_")
    lex = lexicon_lib.LexiconManager(tmp, plugin_dir=BASE)
    words = lex.literal_words()
    print(f"词库字面量词条：{len(words)} 条")

    m = variant.VariantMatcher(BASE)
    t0 = time.perf_counter()
    m.set_words(words)
    build = (time.perf_counter() - t0) * 1000
    st = m.stats()
    print(f"建索引：{build:.0f}ms（拼音词条 {st['indexed_words']}，跳过 {st['skipped_words']}，"
          f"形近 {st['confusable_groups']} 组）")

    # 扫描耗时
    sample = "今天天气不错啊大家" * 4
    for _ in range(3):
        m.scan(sample)
    t0 = time.perf_counter()
    N = 50
    for _ in range(N):
        m.scan(sample)
    per = (time.perf_counter() - t0) / N * 1000
    print(f"单条扫描（{len(sample)} 字）：{per:.1f}ms")

    # 误报实测：对比几种策略
    print("\n正常群聊句子的误报检查（共 %d 句）：" % len(NORMAL))
    for label, kw in [
        ("默认（>=3 音节 + 不允许模糊）", dict(min_tokens=3, allow_fuzzy=False)),
        ("激进（>=2 音节 + 允许模糊）", dict(min_tokens=2, allow_fuzzy=True)),
    ]:
        bad = []
        for s in NORMAL:
            r = m.scan(s, **kw)
            if r:
                bad.append((s, [(h["method"], h["category"], h["strength"]) for h in r]))
        print(f"  [{label}] 误报 {len(bad)}/{len(NORMAL)} 句（{len(bad)/len(NORMAL)*100:.0f}%）")
        for s, info in bad[:6]:
            print(f"      ⚠️ {s} -> {info}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
