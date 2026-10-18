"""变体检测：谐音 / 形近 / 插入符号等规避写法的「疑似」信号。

定位（很重要）：
    本模块只产出**疑似**信号，不产出「违规」结论，也不执行任何动作。
    上层（main.py）拿到命中后决定是「记录」「撤回」还是「进待办」；
    **永不据此自动禁言**——惩罚性动作只由高置信规则或人工决定。

三个通道：
1. pinyin（谐音）：消息与词条都切成「音节序列」再做索引匹配。
   切分时丢掉标点/空格/表情，所以「加·微·信」「加 微 信」这类插符号写法
   会自然还原；多音字按候选集匹配；词长 ≥3 允许 1 个音节的多/少/替换，
   词长 =2 要求完全一致（否则误报太多）。
2. confusable（形近）：形近字归一化（并查集合并同组字）后做字面匹配，
   覆盖「部首替换 / 笔画增减」这类同形不同音的改写。
3. normalize（归一化）：全角转半角、大小写统一——三个通道共用。

工程约束：
- **零第三方依赖**：拼音表是构建期产物 data/pinyin_map.json（tools/build_pinyin_map.py），
  运行时只查表；数据缺失/损坏时自动降级（只跑剩余通道），绝不抛异常打断审核。
- 词条来源复用 lexicon 模块的字面量索引，不重复维护词表。
- 命中结果里带 `match`（命中的原文片段）仅用于即时判断与日志排查，
  **不落库、不进面板**（与面板「不展示词条内容」的既有声明保持一致）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from typing import Any, Optional

from . import lexicon as lexicon_lib

#: 只保留「汉字单字」与「字母数字串」两类 token，其余（标点/符号/表情/空白）全部丢弃
_CJK_ONE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]|[0-9a-z]+")

#: 参与拼音匹配的词条长度上限（音节数），太长没意义也费时间
_MAX_TOKENS = 12
#: 单条消息参与检测的最大长度
_MAX_TEXT = 600
#: 单次扫描的校验次数上限（常见音节上候选多，用预算兜底，防拖慢审核）
_MAX_ATTEMPTS = 4000

#: 命中强度阈值：词长（音节数）>= 该值且无编辑距离 -> high
_HIGH_TOKENS = 3
#: 默认参与拼音匹配的最小音节数。**为什么是 3 而不是 2**：中文双音节同音词极多，
#: 用「2 字词的拼音」去匹配等于拿一张巨大的同音表碰运气——实测正常群聊句子的误报率
#: 会到两成以上（例：正常短语的拼音与某条违禁词音节完全一致）。所以默认只对 >=3 音节的
#: 词条做谐音匹配，2 字词交给字面层（词库本身）与形近通道。
_DEFAULT_MIN_TOKENS = 3
#: 「整条消息就是一个短词的同音写法」时允许的最短音节数。
#: 单独放行这一种形态：像「似冯」这样**整条消息只有 2 个字、且与某条词条同音**的情况，
#: 基本不存在正常表达撞车（正常句子不会只有一个双音节词），实测不增加误报；
#: 而嵌在句子中间的 2 字同音词依然不匹配（那才是误报重灾区）。
_MIN_WHOLE_TOKENS = 2
#: 默认是否允许「多/少/替换 1 个音节」的模糊匹配。默认**不允许**：实测它是误报主要来源
#: （4 音节窗口允许 1 处差异时，普通句子很容易撞上违禁词）。需要更激进的召回可在面板打开。
_DEFAULT_ALLOW_FUZZY = False
#: 拼音缩写通道：首字母串最多展开多少位。
_MAX_ABBR = 8
#: 全拼串最多展开多少位（中文词一般不超过 4 字，全拼 24 位足够）。
_MAX_FULL = 24
#: 缩写通道的停用词：这些两三个字母的日常英文/网络用语天天出现，
#: 一旦撞上某个词条的首字母就会稳定误报，直接排除（不影响中文侧的任何判定）。
_ABBR_STOP = frozenset({
    "ok", "gg", "nb", "hi", "no", "up", "go", "so", "we", "us", "it", "is",
    "to", "of", "in", "on", "at", "by", "or", "an", "as", "be", "do", "if",
    "my", "me", "ya", "em", "mm", "dd", "pls", "thx", "wtf", "lol", "omg",
})


def literals_from_pattern(pat: str) -> list[str]:
    """从正则里抠出可用的字面片段（用于把代码内置正则也纳入变体检测）。

    只做保守提取：按 | 和分组符号切开，取长度 >= 2 的「连续汉字/字母数字」片段。
    抠不干净没关系——这里只是补充覆盖，误提取最多多比对一个词。
    """
    out: list[str] = []
    if not pat:
        return out
    # 去掉正则元字符，保留内容
    s = re.sub(r"\\[a-zA-Z]", " ", pat)
    s = s.replace("(?:", " ").replace("(?", " ").replace(")", " ")
    for part in re.split(r"[|\[\]{}()^$*+?.\\\s]+", s):
        for m in re.finditer(r"[\u4e00-\u9fff]{2,}|[0-9A-Za-z]{2,}", part):
            frag = m.group(0)
            if frag not in out:
                out.append(frag)
    return out


class VariantMatcher:
    """词条级变体匹配器。词条索引按「签名」惰性重建，签名不变就复用。"""

    def __init__(self, plugin_dir: str, logger=None):
        self.dir = str(plugin_dir)
        self.logger = logger
        self._assets_loaded = False
        self._py_map: dict[str, tuple[str, ...]] = {}
        self._cf_map: dict[str, str] = {}
        self._cf_groups = 0
        # 索引
        self._sig: Optional[str] = None
        self._py_bigram: dict[str, list[int]] = {}
        self._py_tokens: list[tuple[tuple[str, ...], ...]] = []
        self._py_meta: list[tuple] = []
        self._cf_index: Any = None
        self._cf_meta: list[tuple] = []
        # 拼音缩写通道索引：首字母串 / 全拼串 -> meta
        self._abbr_index: dict[str, list[tuple]] = {}
        self._full_index: dict[str, list[tuple]] = {}
        self._build_ms = 0.0
        self._skipped_words = 0

    # ------------------------------------------------------------------
    # 资源加载
    # ------------------------------------------------------------------
    def _load_assets(self) -> None:
        if self._assets_loaded:
            return
        self._assets_loaded = True
        self._load_pinyin()
        self._load_confusables()

    def _load_pinyin(self) -> None:
        path = os.path.join(self.dir, "data", "pinyin_map.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            chars = (data or {}).get("chars") or {}
            self._py_map = {k: tuple(str(v).split("|")) for k, v in chars.items() if v}
            if self._py_map and self.logger:
                self.logger.debug(f"[变体] 拼音表已加载：{len(self._py_map)} 字")
        except Exception as e:
            self._py_map = {}
            if self.logger:
                self.logger.warning(f"[变体] 拼音表不可用，谐音通道已降级关闭：{e}")

    def _load_confusables(self) -> None:
        path = os.path.join(self.dir, "data", "variant_confusables.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            groups = (data or {}).get("groups") or []
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[变体] 形近表不可用，形近通道已降级关闭：{e}")
            return
        # 并查集：同一个字出现在多组里也不会打架
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:      # 路径压缩
                parent[x], x = root, parent[x]
            return root

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for g in groups:
            if not isinstance(g, list):
                continue
            chars = [str(c).strip() for c in g if str(c).strip()]
            if len(chars) < 2:
                continue
            self._cf_groups += 1
            for c in chars[1:]:
                union(chars[0], c)
        self._cf_map = {c: find(c) for c in parent}
        # 归一字 = 组内第一个出现的字（find 结果就是它）
        if self._cf_map and self.logger:
            self.logger.debug(f"[变体] 形近表已加载：{self._cf_groups} 组 / {len(self._cf_map)} 字")

    # ------------------------------------------------------------------
    # 分词 / 归一化
    # ------------------------------------------------------------------
    def _tokenize(self, text: str) -> list[tuple[str, ...]]:
        """文本 -> token 序列；每个 token 是一个「候选读音集」。"""
        if not text:
            return []
        norm = unicodedata.normalize("NFKC", str(text)).lower()
        out: list[tuple[str, ...]] = []
        for m in _TOKEN_RE.finditer(norm):
            tok = m.group(0)
            if len(tok) == 1 and _CJK_ONE.match(tok):
                syl = self._py_map.get(tok)
                if not syl:
                    continue          # 拼音表里没有的字：跳过（少一个音节不影响整体匹配）
                out.append(syl)
            else:
                out.append((tok,))    # 字母数字串按原样
        return out

    def _canon(self, text: str) -> str:
        """形近归一化（只对汉字做，字母数字保持原样）。"""
        if not text or not self._cf_map:
            return text
        return "".join(self._cf_map.get(ch, ch) for ch in text)

    def _word_pinyin(self, word: str) -> Optional[tuple[str, str]]:
        """整词 -> (首字母串, 全拼串)；遇到无拼音的字就放弃（宁缺勿错）。"""
        ini: list[str] = []
        full: list[str] = []
        for ch in str(word or ""):
            if not _CJK_ONE.match(ch):
                return None
            syl = self._py_map.get(ch)
            if not syl or not syl[0]:
                return None
            ini.append(syl[0][0])
            full.append(syl[0])
        if not ini:
            return None
        return "".join(ini), "".join(full)

    # ------------------------------------------------------------------
    # 词条索引
    # ------------------------------------------------------------------
    def _signature(self, words: list[tuple[str, tuple]]) -> str:
        h = hashlib.sha1()
        for w, meta in words:
            h.update(str(w).encode("utf-8", "ignore"))
            h.update(b"\x00")
            h.update(str(meta[0] if meta else "").encode("utf-8", "ignore"))
            h.update(b"\x01")
        return f"{len(words)}:{h.hexdigest()[:16]}"

    def set_words(self, words: list[tuple[str, tuple]]) -> None:
        """喂入词条（来自 lexicon 的字面量索引导出）。签名不变则不重建。"""
        self._load_assets()
        sig = self._signature(words or [])
        if sig == self._sig:
            return
        t0 = time.perf_counter()
        self._py_bigram = {}
        self._py_tokens = []
        self._py_meta = []
        self._skipped_words = 0
        self._abbr_index = {}
        self._full_index = {}
        cf_index = lexicon_lib.LiteralIndex()
        self._cf_meta = []
        for idx, (word, meta) in enumerate(words or []):
            w = str(word or "").strip()
            if not w:
                continue
            # --- 拼音通道 ---
            if self._py_map:
                toks = self._tokenize(w)
                if len(toks) < 2:
                    self._skipped_words += 1        # 单字/纯符号词条不参与
                elif len(toks) > _MAX_TOKENS:
                    self._skipped_words += 1
                else:
                    wi = len(self._py_tokens)
                    self._py_tokens.append(tuple(toks))
                    self._py_meta.append(meta)
                    seen: set[str] = set()
                    for i in range(len(toks) - 1):
                        for a in toks[i]:
                            for b in toks[i + 1]:
                                key = a + "\x00" + b
                                if key in seen:
                                    continue
                                seen.add(key)
                                self._py_bigram.setdefault(key, []).append(wi)
            # --- 拼音缩写通道（jwx / jiaweixin）---
            if self._py_map and 2 <= len(w) <= 8:
                pair = self._word_pinyin(w)
                if pair:
                    ab, fu = pair
                    self._abbr_index.setdefault(ab, []).append(meta)
                    self._full_index.setdefault(fu, []).append(meta)
            # --- 形近通道 ---
            if self._cf_map:
                cw = self._canon(w.lower())
                if cw != w.lower() and len(cw) >= 2:
                    cf_index.add(cw, meta)
        self._cf_index = cf_index if len(cf_index) else None
        self._sig = sig
        self._build_ms = round((time.perf_counter() - t0) * 1000, 1)
        if self.logger:
            self.logger.info(
                f"[变体] 索引已重建：词条 {len(self._py_meta)}（拼音）/ {len(cf_index)}（形近），"
                f"跳过 {self._skipped_words}，耗时 {self._build_ms}ms"
            )

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------
    @staticmethod
    def _min_edits(kw: tuple[tuple[str, ...], ...], seg: list[tuple[str, ...]]) -> int:
        """序列间最小编辑距离；替换代价 0/1（候选读音集有交集即视为相同）。"""
        n, m = len(kw), len(seg)
        if abs(n - m) > 1:
            return 99
        prev = list(range(m + 1))
        for i in range(1, n + 1):
            cur = [i] + [0] * m
            for j in range(1, m + 1):
                sub = 0 if (set(kw[i - 1]) & set(seg[j - 1])) else 1
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + sub)
            prev = cur
        return prev[m]

    def _scan_pinyin(self, norm_text: str, extra: list[tuple[str, tuple]],
                     allow_l: set[str], limit: int,
                     min_tokens: int = _DEFAULT_MIN_TOKENS,
                     allow_fuzzy: bool = _DEFAULT_ALLOW_FUZZY) -> list[dict]:
        if not self._py_map:
            return []
        raw_tokens: list[tuple[str, ...]] = []
        spans: list[tuple[int, int]] = []
        low = unicodedata.normalize("NFKC", str(norm_text)).lower()
        for m in _TOKEN_RE.finditer(low):
            tok = m.group(0)
            if len(tok) == 1 and _CJK_ONE.match(tok):
                syl = self._py_map.get(tok)
                if not syl:
                    continue
                raw_tokens.append(syl)
            else:
                raw_tokens.append((tok,))
            spans.append((m.start(), m.end()))
        if len(raw_tokens) < 2:
            return []

        out: list[dict] = []
        tried: set[tuple[int, int]] = set()     # (词条下标, 起点) 去重
        attempts = 0
        budget = _MAX_ATTEMPTS                 # 防止常见音节上候选爆炸

        def _emit(wi: int, start: int, toks, meta) -> None:
            """校验「词条 toks 从消息 start 处开始」是否成立，成立则产出命中。"""
            n = len(toks)
            whole_msg = start == 0 and n == len(raw_tokens)   # 命中片段覆盖整条消息
            if n < min_tokens and not (n >= _MIN_WHOLE_TOKENS and whole_msg):
                # 太短的词条不做谐音匹配（同音词撞车）；例外见 _MIN_WHOLE_TOKENS 注释
                return
            for w in (n, n + 1, n - 1):
                if w <= 0 or start + w > len(raw_tokens):
                    continue
                seg = raw_tokens[start : start + w]
                if w == n and all(set(toks[k]) & set(seg[k]) for k in range(n)):
                    edits = 0
                elif allow_fuzzy and n >= 3:
                    edits = self._min_edits(toks, seg)
                    if edits > 1:
                        continue
                else:
                    continue
                st, en = spans[start][0], spans[start + w - 1][1]
                raw = low[st:en]
                if not raw or raw in allow_l:
                    return
                if raw == str(meta[1] if len(meta) > 1 else "").lower():
                    return                       # 与词条字面一致：归原词库层，不算变体
                strength = "high" if (edits == 0 and (n >= _HIGH_TOKENS or whole_msg)) \
                    else ("medium" if n >= _HIGH_TOKENS else "low")
                out.append({
                    "method": "pinyin",
                    "category": str(meta[0] or ""),
                    "source": str(meta[3] if len(meta) > 3 else ""),
                    "strength": strength,
                    "edits": edits,
                    "span": [st, en],
                    "match": raw[:40],
                })
                return

        # 按消息位置对齐：位置 i 的音节 bigram 反查词条，再按词条内 bigram 序号回推起点
        for i in range(len(raw_tokens) - 1):
            if len(out) >= limit or attempts > budget:
                break
            for a in raw_tokens[i]:
                for b in raw_tokens[i + 1]:
                    for wi in self._py_bigram.get(a + "\x00" + b, ()):
                        toks = self._py_tokens[wi]
                        meta = self._py_meta[wi]
                        for k in range(len(toks) - 1):
                            start = i - k
                            if start < 0:
                                continue
                            key = (wi, start)
                            if key in tried:
                                continue
                            tried.add(key)
                            attempts += 1
                            _emit(wi, start, toks, meta)
                            if attempts > budget or len(out) >= limit:
                                break
                        if attempts > budget or len(out) >= limit:
                            break
        for word, meta in extra:
            if len(out) >= limit:
                break
            toks = tuple(self._tokenize(word))
            n_toks = len(toks)
            if n_toks < 2:
                continue
            starts: range | list[int] = range(max(0, len(raw_tokens) - n_toks - 1))
            if not starts and len(raw_tokens) == n_toks:
                starts = [0]             # 整条消息恰好就是这个（短）自定义词
            for start in starts:
                _emit(0, start, toks, meta)
                if len(out) >= limit:
                    break
        return out

    def _scan_confusable(self, norm_text: str, extra: list[tuple[str, tuple]],
                         allow_l: set[str], limit: int) -> list[dict]:
        if not self._cf_map:
            return []
        low = unicodedata.normalize("NFKC", str(norm_text)).lower()
        canon = self._canon(low)
        out: list[dict] = []
        items: list[tuple[str, tuple]] = []
        if self._cf_index is not None:
            for start, end, info, word in self._cf_index.search(canon, limit=limit * 4):
                items.append((word, info))
        for word, meta in extra:
            cw = self._canon(str(word).lower())
            if cw != str(word).lower() and cw in canon:
                items.append((str(word), meta))
        for word, meta in items:
            cw = self._canon(str(word).lower())
            pos = canon.find(cw)
            if pos < 0:
                continue
            raw = low[pos : pos + len(cw)]
            if not raw or raw == str(word).lower() or raw in allow_l:
                continue
            if str(word).lower() in allow_l:
                continue
            out.append({
                "method": "confusable",
                "category": str(meta[0] or ""),
                "source": str(meta[3] if len(meta) > 3 else ""),
                "strength": "high" if len(cw) >= _HIGH_TOKENS else "low",
                "edits": 0,
                "span": [pos, pos + len(cw)],
                "match": raw[:40],
            })
            if len(out) >= limit:
                break
        return out

    def _scan_abbr(self, norm_text: str, extra: list[tuple[str, tuple]],
                   allow_l: set, limit: int) -> list[dict]:
        """拼音缩写通道：把字母串还原成「首字母串 / 全拼串」再比对。

        覆盖的手法：「加微信」写成 `jwx` / `jiaweixin` / `vx`。
        精度控制（这条通道天生误报偏高，所以限制得很死）：
        - 两位缩写必须**独占**整个字母串（避免 wifi、ok 这类词里撞上）；
        - 全拼串长度必须 ≥ 4；
        - 结果一律标 `strength=low`，只进待办，不参与是否违规的判定。
        """
        if not norm_text:
            return []
        out: list[dict] = []
        for m in re.finditer(r"[a-z]{2,}", norm_text):
            run = m.group(0)
            base = m.start()
            seen: set[str] = set()
            for length in range(2, min(len(run), _MAX_FULL) + 1):
                for i in range(0, len(run) - length + 1):
                    sub = run[i:i + length]
                    if sub in seen:
                        continue
                    metas = None
                    if length <= _MAX_ABBR:
                        metas = self._abbr_index.get(sub)
                    if not metas and length >= 4:
                        metas = self._full_index.get(sub)
                    if not metas:
                        continue
                    if length == 2 and len(run) != 2:
                        continue          # 两位缩写只在「整条都是它」时才认
                    if sub in _ABBR_STOP:
                        continue          # 日常英文/网络用语，稳定误报源
                    meta = metas[0]
                    if str(meta[1]).lower() in allow_l:
                        continue
                    seen.add(sub)
                    out.append({
                        "method": "abbr",
                        "category": meta[0],
                        "source": meta[3] if len(meta) > 3 else "",
                        "strength": "low",
                        "edits": 0,
                        "span": [base + i, base + i + length],
                        "match": sub,
                    })
                    if len(out) >= limit:
                        return out
        # 本群自定义词条：数量少，直接算缩写再在正文里找
        for word, meta in extra or []:
            pair = self._word_pinyin(word)
            if not pair:
                continue
            for cand in pair:
                if len(cand) < 3:
                    continue
                pos = norm_text.find(cand)
                if pos < 0:
                    continue
                if str(meta[1]).lower() in allow_l:
                    continue
                out.append({
                    "method": "abbr",
                    "category": meta[0],
                    "source": meta[3] if len(meta) > 3 else "",
                    "strength": "low",
                    "edits": 0,
                    "span": [pos, pos + len(cand)],
                    "match": cand,
                })
                break
            if len(out) >= limit:
                break
        return out

    def scan(self, text: str, *, extra_words: Optional[list[tuple[str, tuple]]] = None,
             allow: Optional[set] = None, limit: int = 5,
             pinyin: bool = True, confusable: bool = True, abbr: bool = True,
             min_tokens: int = _DEFAULT_MIN_TOKENS,
             allow_fuzzy: bool = _DEFAULT_ALLOW_FUZZY) -> list[dict]:
        """扫描一条消息，返回疑似变体命中（可能为空）。

        extra_words: 本群自定义词条 [(词, (类型, 词, 来源id, 来源名))]，线性比对（数量少）。
        allow: 白名单词（命中即跳过）。
        """
        if not text:
            return []
        t = str(text)[:_MAX_TEXT]
        self._load_assets()
        if not self._py_map and not self._cf_map:
            return []
        allow_l = {str(a).strip().lower() for a in (allow or set()) if str(a).strip()}
        extra = list(extra_words or [])
        out: list[dict] = []
        if pinyin and self._py_map:
            out += self._scan_pinyin(t, extra, allow_l, limit,
                                     min_tokens=int(min_tokens or 2), allow_fuzzy=bool(allow_fuzzy))
        if confusable and self._cf_map and len(out) < limit:
            out += self._scan_confusable(t, extra, allow_l, max(1, limit - len(out)))
        if abbr and self._py_map and len(out) < limit:
            norm_text = unicodedata.normalize("NFKC", str(t)).lower()
            out += self._scan_abbr(norm_text, extra, allow_l, max(1, limit - len(out)))
        # 去重（同一位置同一方法只留一条）+ 重叠裁剪（同一处只报一次，长的优先）
        uniq: list[dict] = []
        for h in sorted(out, key=lambda x: (x["span"][0], -(x["span"][1] - x["span"][0]))):
            span = h.get("span") or [0, 0]
            if any(not (span[1] <= u["span"][0] or span[0] >= u["span"][1]) for u in uniq):
                continue
            if any((h["method"] == u["method"] and h["span"] == u["span"]) for u in uniq):
                continue
            uniq.append(h)
            if len(uniq) >= limit:
                break
        return uniq

    # ------------------------------------------------------------------
    # 状态（给 /审核 状态 与面板用）
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        self._load_assets()
        return {
            "pinyin_ready": bool(self._py_map),
            "pinyin_chars": len(self._py_map),
            "confusable_ready": bool(self._cf_map),
            "confusable_groups": self._cf_groups,
            "confusable_chars": len(self._cf_map),
            "indexed_words": len(self._py_meta),
            "abbr_indexed": len(self._abbr_index),
            "skipped_words": self._skipped_words,
            "build_ms": self._build_ms,
        }
