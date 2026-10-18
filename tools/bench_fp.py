#!/usr/bin/env python3
"""变体检测误报压测：正常聊天 + 常见英文/网络缩写，对比各通道开关的命中数。"""
from __future__ import annotations
import os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(BASE))
from _plugin_import import load  # noqa

variant = load("variant")
lx = load("lexicon")

words = lx.LexiconManager(os.path.join('/tmp', 'bench_pd'), plugin_dir=BASE).literal_words()
m = variant.VariantMatcher(BASE)
m.set_words(words)

CORPUS = [
    # 日常闲聊
    "今天天气不错，下午去打球吗", "这个方案我觉得可以再讨论一下",
    "晚饭吃什么，食堂还是外卖", "明天早上八点开会别忘了",
    "昨天那个电影挺好看的，推荐", "作业写到一半卡住了救命",
    "周末有人一起去图书馆吗", "这周的报告我周五交",
    "新版本更新了吗，我怎么没收到", "考试范围老师说了吗",
    "路上堵车了，晚十分钟到", "你那条消息我没看懂，再说一遍",
    # 科技 / 数码
    "这个 CPU 跑分还挺高的", "我换了新显卡，GPU 温度控制不错",
    "python 那个库装不上，报错了", "手机 app 更新后变卡了",
    "家里 wifi 又断了", "iPhone 电池健康掉到八十了",
    "这个算法的时间复杂度是 O(n log n)", "服务器内存不够了要扩容",
    "docker 镜像拉不下来", "git 合并冲突怎么解决",
    # 英文 / 网络缩写（缩写通道的主要误报来源）
    "ok 收到", "good job", "thanks a lot", "hello 有人吗",
    "lol 这也太搞笑了", "gg 打完了", "nb 厉害啊",
    "yyds 真的绝了", "xswl 笑死我了", "awsl 太可爱了",
    "tql 膜拜大佬", "u1s1 我觉得还行", "nsdd 你说得对",
    "dbq 我的错", "bhys 打扰了", "zqsg 追星",
    "fyi 会议纪要发你了", "asap 尽快处理", "btw 顺便说一句",
    "imo 这个设计有点问题", "tbh 我不太喜欢这个方案",
    # 混合
    "PDF 发我一下谢谢", "这个 PPT 排版不错", "Excel 公式怎么写",
    "API 文档在哪", "SDK 更新了吗", "URL 贴一下",
    "我用 MAC 打开乱码了", "这个 BUG 复现不了",
    "代码 review 过了吗", "commit 信息写清楚点",
]

def run(**kw):
    hits = 0
    detail = []
    for text in CORPUS:
        try:
            res = m.scan(text, **kw)
        except Exception as e:
            res = [{"method": f"异常:{type(e).__name__}"}]
        if res:
            hits += 1
            detail.append((text, [r.get("method") for r in res]))
    return hits, detail

configs = [
    ("全关（只看字面/谐音基线）", dict(abbr=False, confusable=False)),
    ("仅谐音", dict(abbr=False, confusable=False)),
    ("谐音+形近", dict(abbr=False, confusable=True)),
    ("谐音+形近+缩写（当前默认）", dict(abbr=True, confusable=True)),
]

print(f"误报语料：{len(CORPUS)} 条正常消息\n")
print(f"{'配置':<30}{'误报条数':>8}{'误报率':>9}")
print('-' * 50)
for label, kw in configs:
    n, detail = run(**kw)
    print(f"{label:<30}{n:>8}{n/len(CORPUS)*100:>8.0f}%")
    for text, methods in detail[:6]:
        print(f"      · {text}  -> {methods}")
    print()
