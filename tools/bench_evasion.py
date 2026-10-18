#!/usr/bin/env python3
"""变体检测能力基准：自动生成各类规避写法，统计各通道召回率（离线）。"""
from __future__ import annotations
import json, os, sys, random
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(BASE))
from _plugin_import import load  # noqa

variant = load("variant")

random.seed(42)
D = BASE

# ---------- 1) 装配真实词表 ----------
words = []
try:
    lx = load("lexicon")
    mgr = lx.LexiconManager(os.path.join('/tmp', 'bench_pd'), plugin_dir=D)
    words.extend(mgr.literal_words())
except Exception as e:
    print(f"词库加载失败：{e}")

m = variant.VariantMatcher(D)
m.set_words(words)
st = m.stats()
print(f"索引：{st['indexed_words']} 词 | 拼音 {st['pinyin_chars']} 字 | 形近 {st['confusable_groups']} 组 | 建表 {st['build_ms']}ms\n")

# ---------- 2) 取目标词（只用广告/辱骂类，避开敏感类别） ----------
targets = []
for w, meta in words:
    cat = meta[0] if isinstance(meta, (tuple, list)) else ''
    if cat in ('ad', 'abuse') and 2 <= len(w) <= 4 and all('\u4e00' <= c <= '\u9fff' for c in w):
        targets.append(w)
targets = list(dict.fromkeys(targets))
random.shuffle(targets)
targets = targets[:60]
print(f"抽样目标词：{len(targets)} 个（ad/abuse 类）\n")

# ---------- 3) 变体生成器 ----------
_raw_py = json.load(open(os.path.join(D, 'data', 'pinyin_map.json'), encoding='utf-8'))
py = {k: v.split('|') for k, v in (_raw_py.get('chars') or {}).items()}
conf = json.load(open(os.path.join(D, 'data', 'variant_confusables.json'), encoding='utf-8'))

# 形近字反查：字 -> 同组其他字
conf_groups = conf.get('groups', []) if isinstance(conf, dict) else []
conf_map = defaultdict(list)
for g in conf_groups:
    for ch in g:
        conf_map[ch].extend([x for x in g if x != ch])

def gen_symbol_insert(w):
    return list(w)[0] + '·' + '·'.join(list(w)[1:])
def gen_space_insert(w):
    return ' '.join(list(w))
def gen_fullwidth(w):
    return ''.join(chr(ord(c) + 0xFEE0) if '!' <= c <= '~' else c for c in w)
def gen_upper(w):
    return w.upper()
def gen_confusable(w):
    for i, c in enumerate(w):
        if conf_map.get(c):
            return w[:i] + random.choice(conf_map[c]) + w[i+1:]
    return None
_PY_INDEX = defaultdict(list)
for _ch, _ps in py.items():
    for _p in _ps:
        _PY_INDEX[_p].append(_ch)

def gen_homophone(w):
    for i, c in enumerate(w):
        ps = py.get(c)
        if not ps:
            continue
        cands = []
        for _p in ps:
            cands.extend([x for x in _PY_INDEX[_p] if x != c])
        cands = [x for x in dict.fromkeys(cands) if len(x) == 1]
        if cands:
            return w[:i] + random.choice(cands) + w[i+1:]
    return None
def gen_pinyin_initial(w):
    out = []
    for c in w:
        p = py.get(c)
        if not p:
            return None
        out.append(p[0][0])
    return ''.join(out) or None

def gen_pinyin_full(w):
    out = []
    for c in w:
        p = py.get(c)
        if not p:
            return None
        out.append(p[0])
    return ''.join(out) or None

def gen_emoji_insert(w):
    return list(w)[0] + '🤝' + '🤝'.join(list(w)[1:])
def gen_mid_char(w):
    if len(w) < 3:
        return None
    i = len(w) // 2
    return w[:i] + random.choice('的了个啊吧呢') + w[i:]

TECHNIQUES = [
    ('插 emoji', gen_emoji_insert),
    ('中间插字', gen_mid_char),
    ('插符号 ·', gen_symbol_insert),
    ('插空格', gen_space_insert),
    ('全角字符', gen_fullwidth),
    ('大写', gen_upper),
    ('形近字替换', gen_confusable),
    ('同音字替换', gen_homophone),
    ('拼音首字母', gen_pinyin_initial),
    ('拼音全拼', gen_pinyin_full),
]

# ---------- 4) 跑基准 ----------
print(f"{'规避手法':<14}{'样本':>5}{'命中':>5}{'召回':>8}   命中通道")
print('-' * 62)
total_hit = total_n = 0
for label, fn in TECHNIQUES:
    hits = n = 0
    methods = defaultdict(int)
    for w in targets:
        v = fn(w)
        if not v or v == w:
            continue
        n += 1
        try:
            res = m.scan(v)
        except Exception:
            res = []
        if res:
            hits += 1
            for r in res:
                methods[r.get('method', '?')] += 1
    total_hit += hits; total_n += n
    rate = f"{hits/n*100:.0f}%" if n else "n/a"
    print(f"{label:<14}{n:>5}{hits:>5}{rate:>8}   {dict(methods)}")

print('-' * 62)
print(f"{'合计':<14}{total_n:>5}{total_hit:>5}{total_hit/total_n*100:>7.0f}%")
