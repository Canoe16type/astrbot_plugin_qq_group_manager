#!/usr/bin/env python3
"""用公开开源词库生成插件内置词库（lexicons/*.json + lexicons/SOURCES.md）。

为什么要有这个脚本：
    内置词库不手写，全部来自公开开源项目。脚本把「上游词库」转成插件格式，
    并把来源、协议、词条数记到 SOURCES.md，任何人可复现、可升级、可溯源。

用法：
    python3 tools/build_lexicons.py            # 联网下载（默认缓存到 tools/.cache）
    python3 tools/build_lexicons.py --offline  # 只用缓存，不联网

上游（全部为公开开源仓库）：
    - konsheng/Sensitive-lexicon       MIT          https://github.com/konsheng/Sensitive-lexicon
    - houbb/sensitive-word-data        Apache-2.0   https://github.com/houbb/sensitive-word-data
    - censor-text/profanity-list       Unlicense    https://github.com/censor-text/profanity-list

词条只做「拦截/审核」用途，插件不展示词条内容。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
OUT_DIR = os.path.join(PLUGIN_DIR, "lexicons")
CACHE_DIR = os.path.join(HERE, ".cache")

UA = {"User-Agent": "qq-group-manager-lexicon-builder/1.0"}

#: 写进每个词库 JSON 的用途声明（自带解释，文件被单独拷走也能自证用途）
NOTICE = '本文件是内容审核用的黑名单词表（不是可发布内容）：来源公开开源（见 DISCLAIMER.md / SOURCES.md），仅在本机用于内容过滤与拦截，不对外发布、不对外提供、不生成、不传播；词条由上游社区维护，不代表插件作者立场。请勿删除本字段。'


# ---------------------------------------------------------------------------
# 上游定义：每个内置词库 = 若干上游文件的并集
# ---------------------------------------------------------------------------

GITHUB_RAW = {
    "konsheng": "https://raw.githubusercontent.com/konsheng/Sensitive-lexicon/main/",
    "houbb": "https://raw.githubusercontent.com/houbb/sensitive-word-data/main/src/main/resources/",
    "censor": "https://raw.githubusercontent.com/censor-text/profanity-list/main/",
}
UPSTREAM = {
    "konsheng/Sensitive-lexicon": ("MIT", "https://github.com/konsheng/Sensitive-lexicon"),
    "houbb/sensitive-word-data": ("Apache-2.0", "https://github.com/houbb/sensitive-word-data"),
    "censor-text/profanity-list": ("Unlicense (public domain)", "https://github.com/censor-text/profanity-list"),
}

#: houbb 词库的标签码 -> 中文类别（见 sensitive-word 源码 WordTagType）
HOUBB_TAGS = {"0": "政治", "1": "毒品", "2": "色情", "3": "赌博", "4": "违法犯罪"}

LEXICONS: list[dict] = [
    {
        "file": "ad.json",
        "name": "广告引流",
        "category": "ad",
        "default_enabled": False,
        "description": "公开词库合并：网络垃圾信息/营销引流关键词。含「淘宝、网络、客服、招聘」等宽泛词，"
                       "误伤率较高，默认关闭；启用前建议先在面板「词典 → 命中测试」里跑一遍并配好白名单",
        "parts": [("konsheng", "Vocabulary/广告类型.txt")],
    },
    {
        "file": "abuse.json",
        "name": "辱骂攻击",
        "category": "abuse",
        "default_enabled": True,
        "description": "公开词库合并：中文脏话/人身攻击词表（来源见 SOURCES.md）",
        "parts": [("censor", "list/zh.txt")],
    },
    {
        "file": "porn.json",
        "name": "色情低俗",
        "category": "porn",
        "default_enabled": False,
        "description": "公开词库合并：色情、低俗、涉黄引流词。词量大且含「电影、姐姐、妹妹、按摩」等常用词，"
                       "误伤率较高，默认关闭；建议配好白名单后再启用",
        "parts": [
            ("konsheng", "Vocabulary/色情类型.txt"),
            ("konsheng", "Vocabulary/色情词库.txt"),
            ("houbb", "@tag:2"),
        ],
    },
    {
        "file": "gambling.json",
        "name": "赌博诈骗",
        "category": "illegal",
        "default_enabled": True,
        "description": "公开词库合并：赌博、博彩、下注等违法引流词（来源见 SOURCES.md）",
        "parts": [("houbb", "@tag:3")],
    },
    {
        "file": "weapon.json",
        "name": "涉枪涉爆违禁",
        "category": "illegal",
        "default_enabled": True,
        "description": "公开词库合并：枪支弹药、爆炸物、违禁品交易词（来源见 SOURCES.md）",
        "parts": [("konsheng", "Vocabulary/涉枪涉爆.txt")],
    },
    {
        "file": "drugs.json",
        "name": "涉毒",
        "category": "illegal",
        "default_enabled": True,
        "description": "公开词库合并：涉毒诱导/交易词，仅用于拦截；现实生活必须远离并坚决抵制毒品（来源见 SOURCES.md）",
        "parts": [("houbb", "@tag:1")],
    },
    {
        "file": "violence.json",
        "name": "暴恐极端",
        "category": "illegal",
        "default_enabled": False,
        "description": "公开词库合并：暴恐、极端主义词表。含「枪手、兴奋剂」等中性词，默认关闭（来源见 SOURCES.md）",
        "parts": [("konsheng", "Vocabulary/暴恐词库.txt")],
    },
    {
        "file": "politics.json",
        "name": "涉政敏感",
        "category": "politics",
        "default_enabled": False,
        "description": "公开词库合并：涉政敏感词表，用于群内合规审核，默认关闭（来源见 SOURCES.md）",
        "parts": [
            ("konsheng", "Vocabulary/政治类型.txt"),
            ("houbb", "@tag:0"),
        ],
    },
    {
        "file": "crime.json",
        "name": "违法犯罪",
        "category": "illegal",
        "default_enabled": False,
        "description": "公开词库合并：违法犯罪类词表（体量大），默认关闭（来源见 SOURCES.md）",
        "parts": [("houbb", "@tag:4")],
    },
]

#: 去重：按 LEXICONS 顺序先到先得，避免一条消息重复命中多个库

SPLIT_RE = re.compile(r"[\n\r,，、;；\t|]+")
JUNK_RE = re.compile(r"^[\W_]+$", re.UNICODE)          # 纯符号
MIN_LEN, MAX_LEN = 2, 40


# ---------------------------------------------------------------------------
# 下载 / 缓存
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.md5(key.encode("utf-8")).hexdigest() + ".txt")


def fetch(url: str, offline: bool = False, ttl: int = 7 * 24 * 3600) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(url)
    fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path) < ttl)
    if fresh or offline:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        if offline:
            raise RuntimeError(f"离线模式且缓存缺失：{url}")
    safe_url = urllib.parse.quote(url, safe=":/?&=#%+")     # 上游文件名可能是中文，必须转义
    req = urllib.request.Request(safe_url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8", errors="ignore")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def clean_words(raw_text: str) -> list[str]:
    out, seen = [], set()
    for w in SPLIT_RE.split(raw_text or ""):
        w = w.strip().strip("\ufeff").strip()
        if not w or len(w) < MIN_LEN or len(w) > MAX_LEN:
            continue
        if JUNK_RE.match(w):
            continue
        if w.lower() in seen:
            continue
        seen.add(w.lower())
        out.append(w)
    return out


# ---------------------------------------------------------------------------
# 上游解析
# ---------------------------------------------------------------------------

_houbb_cache: dict[str, list[str]] = {}


def houbb_by_tag(tag: str) -> list[str]:
    """houbb/sensitive-word-data：`词 标签码列表`，按标签码取词。"""
    if not _houbb_cache:
        raw = fetch(GITHUB_RAW["houbb"] + "sensitive_word_tags.txt")
        buckets: dict[str, list[str]] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            word, codes = parts[0], parts[1]
            for code in codes.split(","):
                buckets.setdefault(code.strip(), []).append(word)
        _houbb_cache.update(buckets)
    return list(_houbb_cache.get(tag, []))


def load_part(source: str, path: str) -> tuple[list[str], str]:
    """返回 (词条, 上游标识)。path 以 @tag:N 开头表示 houbb 标签库。"""
    if path.startswith("@tag:"):
        tag = path.split(":", 1)[1]
        cn = HOUBB_TAGS.get(tag, tag)
        return houbb_by_tag(tag), f"houbb/sensitive-word-data · 标签「{cn}」(tag={tag})"
    text = fetch(GITHUB_RAW[source] + path)
    return clean_words(text), f"{'konsheng/Sensitive-lexicon' if source == 'konsheng' else 'censor-text/profanity-list'} · {path}"


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

def build(offline: bool = False) -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    used: set[str] = set()
    report: list[dict] = []
    for spec in LEXICONS:
        words: list[str] = []
        seen: set[str] = set()
        parts_meta = []
        for source, path in spec["parts"]:
            try:
                items, label = load_part(source, path)
            except Exception as e:                                   # noqa: BLE001
                print(f"  ! 拉取失败，已跳过：{path} -> {e}", file=sys.stderr)
                continue
            parts_meta.append(label)
            kept = 0
            for w in clean_words("\n".join(items)):
                key = w.lower()
                if key in seen or key in used:
                    continue
                seen.add(key)
                words.append(w)
                kept += 1
            print(f"  {spec['file']:<16} <- {label:<60} 取用 {kept}")
        if not words:
            print(f"  ! {spec['file']} 没有任何词条，跳过生成", file=sys.stderr)
            continue
        used |= {w.lower() for w in words}
        payload = {
            "notice": NOTICE,
            "name": spec["name"],
            "description": spec["description"],
            "category": spec["category"],
            "match": "literal",                       # 词库是字面量词表，不做正则解释
            "default_enabled": spec["default_enabled"],
            "author": "开源词库（见 SOURCES.md）",
            "version": time.strftime("%Y.%m%d"),
            "generated_by": "tools/build_lexicons.py",
            "sources": parts_meta,
            "categories": {spec["category"]: sorted(words, key=lambda x: (len(x), x))},
        }
        out = os.path.join(OUT_DIR, spec["file"])
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        report.append({"file": spec["file"], "name": spec["name"], "category": spec["category"],
                       "count": len(words), "enabled": spec["default_enabled"],
                       "sources": parts_meta, "bytes": os.path.getsize(out)})
        print(f"  = {spec['file']:<16} {len(words):>6} 词条")

    write_sources_md(report)
    total = sum(r["count"] for r in report)
    size = sum(r["bytes"] for r in report)
    print(f"\n共 {len(report)} 个内置词库 / {total} 词条 / {size / 1024:.0f} KB")
    return 0


def write_sources_md(report: list[dict]) -> None:
    lines = [
        "# 内置词库来源与数据用途",
        "",
        "> ⚠ **免责与用途声明（本文件由脚本生成，重新生成不会丢失）**",
        ">",
        "> 本目录 `*.json` 是**内容审核用的黑名单词表**，用途是「命中即拦截」，**不是可发布的文本内容**；",
        "> 仅在本机用于内容过滤，**不对外发布、不对外提供、不生成、不传播**，不代表插件作者立场。",
        "> 完整声明见同目录 [`DISCLAIMER.md`](./DISCLAIMER.md)；词条由上游开源社区维护，可能误伤或漏词，",
        "> 是否启用由使用者自行判断并承担责任，作者不对词条准确性与使用后果作任何保证。",
        ">",
        "> 不需要哪一类词库，直接删除对应文件即可（插件不会重新生成、也不会报错）。",
        "",
        "本目录下的 `*.json` **不是手写的**，全部由 `tools/build_lexicons.py` 从公开开源词库生成：",
        "",
        "```bash",
        "python3 tools/build_lexicons.py        # 重新生成（联网，缓存 7 天）",
        "python3 tools/build_lexicons.py --offline",
        "```",
        "",
        "## 上游项目",
        "",
        "| 项目 | 协议 | 地址 |",
        "| --- | --- | --- |",
    ]
    for repo, (lic, url) in UPSTREAM.items():
        lines.append(f"| {repo} | {lic} | {url} |")
    lines += [
        "",
        "## 生成了什么",
        "",
        "| 文件 | 名称 | 违规类型 | 词条 | 默认 | 上游文件 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in report:
        src = "<br>".join(s.replace("|", "/") for s in r["sources"])
        lines.append(f"| `{r['file']}` | {r['name']} | {r['category']} | {r['count']} | "
                     f"{'开' if r['enabled'] else '关'} | {src} |")
    lines += [
        "",
        "## 使用与合规说明",
        "",
        "- 词表仅用于**拦截、审核、过滤**本群违规内容，插件界面不展示词条内容；",
        "- 上游词库由社区维护，可能包含误伤词或漏词：可在管理面板「词典」页启用/停用，",
        "  或上传自己的词库覆盖补充（同名词库按「自定义优先」合并）；",
        "- 协议各不相同（MIT / Apache-2.0 / Unlicense），二次分发时请保留本文件与本表。",
        "- 涉毒、暴恐、涉政、违法犯罪等词表默认关闭或按需开启，开启后请在群里先跑试运行。",
        "",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    with open(os.path.join(OUT_DIR, "SOURCES.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description="从公开开源词库生成插件内置词库")
    ap.add_argument("--offline", action="store_true", help="只用本地缓存，不联网")
    args = ap.parse_args()
    print("从公开词库生成内置词库：")
    return build(offline=args.offline)


if __name__ == "__main__":
    raise SystemExit(main())
