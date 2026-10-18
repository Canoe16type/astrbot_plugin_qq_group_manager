"""关键词词库（四维度判定之一：keyword）管理。

职责：
1. **内置词库**：随插件发布的 `lexicons/*.json`，由 `tools/build_lexicons.py`
   从公开开源词库生成（见 `lexicons/SOURCES.md`），升级插件即更新，用户不可删、只能停用；
2. **自定义词库**：页面上一键上传（JSON / TXT / CSV），落在
   `data/plugin_data/<插件名>/lexicons/` 下，可改名、可下载、可删；
3. **启用状态**：哪些词库参与判定，全局一份（`lexicon_state.json`），页面上勾选；
4. **白名单**：命中白名单词的规则直接跳过，用来快速消掉误伤；
5. **命中测试**：把一句话丢进来，返回命中了哪条规则，方便调试。

关于「词库」与「正则」两类规则的区分（重要）：
- 词库文件里写的是**词**，按**字面量**匹配（`match: "literal"`，公开词库都是这种）；
- 想写正则要显式声明：文件级 `"match": "regex"`、或键名 `regexes`、或单条加前缀 `re:`；
- 用户随手传的 TXT/CSV 默认 `auto`：看不出正则特征的词按字面量处理，避免 `C++`、`1.5折`
  这类词被当正则解释；明显是正则的（含 `\\d`、`.*`、`[..]` 等）才当正则编译。

性能：字面量词条走自建的多模式索引（按首字 + 长度分桶），4 万条词表单条消息
命中检测在毫秒级；正则规则才逐条 `re.search`，所以大规模词表不会被逐条正则拖死。

本模块不 import AstrBot，纯标准库，方便离线单测。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BUILTIN_DIRNAME = "lexicons"
STATE_FILENAME = "lexicon_state.json"

#: 允许的违规类型（与 moderation.DEFAULT_CAT_NAMES 对齐）
CATEGORIES = ("ad", "abuse", "porn", "illegal", "politics", "spam", "other")

#: 中文/常见别名 -> 规范类型
CATEGORY_ALIASES: dict[str, str] = {
    "ad": "ad", "广告": "ad", "引流": "ad", "营销": "ad", "推广": "ad",
    "abuse": "abuse", "辱骂": "abuse", "攻击": "abuse", "人身攻击": "abuse", "脏话": "abuse",
    "porn": "porn", "色情": "porn", "低俗": "porn", "涉黄": "porn", "yellow": "porn",
    "illegal": "illegal", "违法": "illegal", "违法违禁": "illegal", "违禁": "illegal",
    "违规": "illegal", "违法违禁品": "illegal", "涉枪涉爆": "illegal", "赌博": "illegal",
    "诈骗": "illegal", "涉毒": "illegal",
    "politics": "politics", "涉政": "politics", "政治": "politics", "政治敏感": "politics",
    "spam": "spam", "刷屏": "spam", "灌水": "spam", "重复": "spam",
    "other": "other", "其他": "other", "其它": "other",
}

#: 上传限制（公开词库动辄上万条，上限放得比较宽）
MAX_FILE_BYTES = 6 * 1024 * 1024      # 单个词库 6 MB
MAX_PATTERNS = 200000                 # 单库最多规则条数
MAX_NAME_LEN = 60
MAX_PATTERN_LEN = 200
MAX_ERRORS_REPORTED = 30
MAX_PREVIEW = 200                     # 页面预览时每库最多回传的规则条数
RAW_KEEP_BYTES = 400 * 1024           # 超过这个大小就不缓存原文，省内存

#: 判定用正则 flags（忽略大小写 + 多行）
FLAGS = re.I | re.M

SLUG_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fa5_-]+")

#: 匹配模式
MATCH_LITERAL = "literal"
MATCH_REGEX = "regex"
MATCH_AUTO = "auto"

#: 正则特征：出现这些才把 auto 模式下的规则当正则
REGEX_HINT = re.compile(r"\\[a-zA-Z0-9]|\[\^?[^\]]{1,60}\]|\(\?|\.\*|\.\+|\{\d+(?:,\d*)?\}|[\^$]|\|")

#: 规则前缀：显式声明匹配方式
PREFIX_RE = re.compile(r"^(re|regex|正则)\s*[:：]\s*(.+)$", re.I | re.S)
PREFIX_LIT = re.compile(r"^(lit|literal|词|字面量)\s*[:：]\s*(.+)$", re.I | re.S)

#: 白名单中的词默认按字面量处理（允许 `re:` 前缀写正则）


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def slugify(name: str, fallback: str = "lexicon") -> str:
    """把词库名转成安全的文件名（保留中英文/数字/下划线/短横线）。"""
    s = SLUG_RE.sub("-", str(name or "").strip()).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    return (s or fallback)[:40]


def normalize_category(raw: Any) -> str:
    c = str(raw or "").strip().lower()
    return CATEGORY_ALIASES.get(c, c if c in CATEGORIES else "other")


def split_pattern(raw: str, mode: str = MATCH_AUTO) -> tuple[str, bool, Optional[str]]:
    """把一条规则拆成 (文本, 是否正则, 错误)。

    mode: literal / regex / auto，单条规则的 `re:` / `lit:` 前缀优先级最高。
    """
    p = str(raw if raw is not None else "").strip()
    if not p:
        return "", False, "空规则"
    m = PREFIX_RE.match(p)
    if m:
        return m.group(2).strip(), True, None
    m = PREFIX_LIT.match(p)
    if m:
        return m.group(2).strip(), False, None
    if len(p) > MAX_PATTERN_LEN:
        return "", False, f"规则过长（>{MAX_PATTERN_LEN} 字符）"
    if mode == MATCH_LITERAL:
        return p, False, None
    if mode == MATCH_REGEX:
        return p, True, None
    return (p, True, None) if REGEX_HINT.search(p) else (p, False, None)


def risk_of(pattern: str, is_regex: bool) -> Optional[str]:
    """粗查误伤风险，只提示不拦截。"""
    if not pattern:
        return "空规则"
    if is_regex:
        if pattern in (".*", ".+", "^.*$", "^.+$"):
            return "匹配一切内容，会让所有消息都判违规"
        if len(pattern.strip("^$()[]\\")) <= 1:
            return "规则太短（1 个字符），极容易误伤"
        if re.search(r"\((?:[^()\\]|\\.)*[*+]\)[*+]", pattern) or re.search(r"(\.\*|\.\+)[*+]", pattern):
            return "存在嵌套量词，长文本下可能卡顿"
        return None
    if len(pattern) == 1:
        return "单个汉字的词条极容易误伤，建议加白名单或停用该库"
    return None


# ---------------------------------------------------------------------------
# 字面量多模式索引（词库主力）
# ---------------------------------------------------------------------------

class LiteralIndex:
    """字面量词表的多模式匹配。

    结构：首字 -> {词长 -> {词: 元信息}}，命中时按首字和长度直接取表查字典，
    不做逐条正则，几万条词表也是毫秒级。
    """

    __slots__ = ("_buckets", "count", "total_chars")

    def __init__(self) -> None:
        self._buckets: dict[str, dict[int, dict[str, tuple]]] = {}
        self.count = 0
        self.total_chars = 0

    def add(self, word: str, meta: tuple) -> None:
        w = str(word or "").strip()
        if not w:
            return
        key = w.lower()
        table = self._buckets.setdefault(key[0], {}).setdefault(len(key), {})
        if key in table:
            return
        table[key] = meta
        self.count += 1
        self.total_chars += len(key)

    def search(self, text: str, limit: int = 20, allow: Optional[set] = None) -> list[tuple]:
        """返回 [(起点, 终点, 元信息, 命中词)]，最多 limit 条（不保证有序）。"""
        if not text or not self._buckets:
            return []
        low = text.lower()
        buckets = self._buckets
        n = len(low)
        out: list[tuple] = []
        for i in range(n):
            char_tables = buckets.get(low[i])
            if not char_tables:
                continue
            for word_len, table in char_tables.items():
                end = i + word_len
                if end > n:
                    continue
                hit = table.get(low[i:end])
                if hit is None:
                    continue
                word = hit[1]
                if allow and (word in allow or word.lower() in allow):
                    continue
                out.append((i, end, hit, word))
                if len(out) >= limit:
                    return out
        return out

    def __len__(self) -> int:
        return self.count

    def items(self) -> list[tuple[str, tuple]]:
        """导出全部 (词, 元信息)，供变体索引（拼音/形近）复用，避免二次维护词表。"""
        out: list[tuple[str, tuple]] = []
        for table_by_len in self._buckets.values():
            for table in table_by_len.values():
                for _key, meta in table.items():
                    word = str(meta[1]) if len(meta) > 1 else ""
                    if word:
                        out.append((word, meta))
        return out


# ---------------------------------------------------------------------------
# 解析：JSON / TXT / CSV -> {类型: [...]}
# ---------------------------------------------------------------------------

_NAME_KEYS = ("name", "名称", "词库名", "title")
_DESC_KEYS = ("description", "desc", "描述", "说明", "备注")
_VER_KEYS = ("version", "版本")
_AUTHOR_KEYS = ("author", "作者", "维护者", "来源")
_CAT_KEYS = ("categories", "category", "分类", "类型", "cats")
_PAT_KEYS = ("patterns", "keywords", "rules", "规则", "关键词", "words", "list")
_REGEX_KEYS = ("regexes", "regex", "regex_patterns", "正则", "正则规则")
_MATCH_KEYS = ("match", "匹配", "模式")
_WORDS_KEYS = ("words", "词", "allow", "白名单")
_KIND_KEYS = ("kind", "类型")


class _Pool:
    """解析中间态：同名类型下的字面量/正则分开收。"""

    def __init__(self) -> None:
        self.literal: dict[str, list[str]] = {}
        self.regex: dict[str, list[str]] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._seen: set[str] = set()
        self.error_count = 0
        self.warning_count = 0

    def add(self, raw: Any, cat: Any, mode: str, where: str = "", force_regex: bool = False) -> None:
        if not isinstance(raw, (str, int, float)):
            self._note_error(f"{where}跳过非文本规则：{raw!r}")
            return
        pat, is_re, err = split_pattern(str(raw), MATCH_REGEX if force_regex else mode)
        if err:
            self._note_error(f"{where}{err}：{str(raw)[:60]}")
            return
        c = normalize_category(cat)
        key = f"{c}\u0000{'re' if is_re else 'lit'}\u0000{pat}"
        if key in self._seen:
            return
        self._seen.add(key)
        if is_re:
            try:
                re.compile(pat, FLAGS)
            except re.error as e:
                self._note_error(f"{where}正则无效：{pat[:60]}（{e}）")
                return
            self.regex.setdefault(c, []).append(pat)
        else:
            self.literal.setdefault(c, []).append(pat)
        w = risk_of(pat, is_re)
        if w:
            self._note_warning(f"{where}「{pat[:40]}」{w}")

    def _note_error(self, msg: str) -> None:
        self.error_count += 1
        if len(self.errors) < MAX_ERRORS_REPORTED:
            self.errors.append(msg)

    def _note_warning(self, msg: str) -> None:
        self.warning_count += 1
        if len(self.warnings) < MAX_ERRORS_REPORTED:
            self.warnings.append(msg)

    def add_many(self, pats: Any, cat: Any, mode: str, where: str = "", force_regex: bool = False) -> None:
        items = pats if isinstance(pats, (list, tuple, set)) else [pats]
        for raw in items:
            self.add(raw, cat, mode, where, force_regex)

    def result(self, meta: dict) -> dict:
        cats: dict[str, list[str]] = {}
        for c, v in self.literal.items():
            cats.setdefault(c, []).extend(v)
        for c, v in self.regex.items():
            cats.setdefault(c, []).extend(v)
        return {
            "categories": cats,
            "literals": self.literal,
            "regexes": self.regex,
            "errors": self.errors,
            "error_count": self.error_count,
            "warnings": self.warnings,
            "warning_count": self.warning_count,
            "meta": meta,
            "count": sum(len(v) for v in cats.values()),
            "literal_count": sum(len(v) for v in self.literal.values()),
            "regex_count": sum(len(v) for v in self.regex.values()),
        }


def _kind_of(data: dict) -> str:
    for k in _KIND_KEYS:
        v = str(data.get(k) or "").strip().lower()
        if v in ("allow", "白名单", "whitelist"):
            return "allow"
        if v in ("rules", "rule", "词库", "规则"):
            return "rules"
    return "rules"


def _match_mode_of(data: Any) -> str:
    """读文件级 match 声明：literal / regex / auto。"""
    if isinstance(data, dict):
        for k in _MATCH_KEYS:
            v = str(data.get(k) or "").strip().lower()
            if v in ("literal", "字面量", "字面", "word", "words", "词"):
                return MATCH_LITERAL
            if v in ("regex", "正则", "re"):
                return MATCH_REGEX
            if v in ("auto", "自动", "自动识别"):
                return MATCH_AUTO
    return MATCH_AUTO


def parse_json_lexicon(text: str) -> dict:
    """解析 JSON 词库，容忍多种常见结构。"""
    pool = _Pool()
    try:
        data = json.loads(text)
    except Exception as e:
        return {"categories": {}, "literals": {}, "regexes": {}, "errors": [f"JSON 解析失败：{e}"],
                "warnings": [], "meta": {}, "count": 0, "error_count": 1, "warning_count": 0,
                "literal_count": 0, "regex_count": 0}

    meta: dict[str, Any] = {}
    mode = MATCH_AUTO
    kind = "rules"

    if isinstance(data, list):
        pool.add_many(data, "other", mode, "顶层：")
    elif isinstance(data, dict):
        for k in _NAME_KEYS:
            if isinstance(data.get(k), str):
                meta["name"] = data[k].strip()[:MAX_NAME_LEN]
                break
        for k in _DESC_KEYS:
            if isinstance(data.get(k), str):
                meta["description"] = data[k].strip()[:300]
                break
        for k in _VER_KEYS:
            if isinstance(data.get(k), (str, int, float)):
                meta["version"] = str(data[k])[:20]
                break
        for k in _AUTHOR_KEYS:
            if isinstance(data.get(k), str):
                meta["author"] = data[k].strip()[:40]
                break
        if isinstance(data.get("tags"), list):
            meta["tags"] = [str(t)[:20] for t in data["tags"]][:8]
        if "default_enabled" in data:
            meta["default_enabled"] = bool(data.get("default_enabled"))
        if isinstance(data.get("sources"), list):
            meta["sources"] = [str(s)[:160] for s in data["sources"]][:8]
        for k in ("notice", "声明", "disclaimer"):
            if isinstance(data.get(k), str) and data[k].strip():
                meta["notice"] = data[k].strip()[:500]
                break
        for k in ("category", "类型", "默认类型"):
            if isinstance(data.get(k), str) and data[k].strip():
                meta["default_category"] = normalize_category(data[k])
                break

        mode = _match_mode_of(data)
        kind = _kind_of(data)
        default_cat = meta.get("default_category")

        # ① 标准结构：categories / 分类 = {类型: [规则]}
        cats = None
        for k in _CAT_KEYS:
            if isinstance(data.get(k), dict):
                cats = data[k]
                break
        if cats is not None:
            for cat, pats in cats.items():
                pool.add_many(pats, cat, mode, f"[{normalize_category(cat)}] ")
        # ③ 顶层直接写类型键：{"ad": [...], "spam": [...]}
        elif not any(isinstance(data.get(k), list) for k in _PAT_KEYS):
            for k, v in data.items():
                if k in (_NAME_KEYS + _DESC_KEYS + _VER_KEYS + _AUTHOR_KEYS + _MATCH_KEYS
                         + _REGEX_KEYS + _CAT_KEYS + _WORDS_KEYS + _KIND_KEYS
                         + ("tags", "default_enabled", "sources", "notice", "声明", "disclaimer")):
                    continue
                if isinstance(v, list):
                    pool.add_many(v, k, mode, f"[{normalize_category(k)}] ")

        # ② 扁平结构：patterns / keywords / rules = [规则]
        for k in _PAT_KEYS:
            if isinstance(data.get(k), list):
                pool.add_many(data[k], default_cat or "other", mode, "顶层：")
                break

        # ④ 白名单结构：words = ["电影", ...]
        for k in _WORDS_KEYS:
            if isinstance(data.get(k), list):
                pool.add_many(data[k], "allow", MATCH_LITERAL, "白名单：")
                kind = "allow"
                break

        # ⑤ 正则区：regexes / 正则 = {类型: [正则]}
        for k in _REGEX_KEYS:
            v = data.get(k)
            if isinstance(v, dict):
                for cat, pats in v.items():
                    pool.add_many(pats, cat, MATCH_REGEX, f"[{normalize_category(cat)}·正则] ", force_regex=True)
            elif isinstance(v, list):
                pool.add_many(v, default_cat or "other", MATCH_REGEX, "正则：", force_regex=True)
            if v is not None:
                break

    if not pool.literal and not pool.regex:
        pool._note_error("没有解析到任何规则，请检查文件结构")
    res = pool.result(meta)
    res["kind"] = kind
    res["match"] = mode
    return res


def parse_txt_lexicon(text: str, default_category: str = "other", mode: str = MATCH_AUTO) -> dict:
    """解析 TXT 词库（一行一条，支持分节与单行指定类型）。

    - ``#`` / ``//`` 开头整行注释；``[ad]`` / ``[广告]`` 分节
    - ``ad: 加vx`` 单行指定类型；纯词按 `auto` 规则处理（通常就是字面量）
    """
    pool = _Pool()
    cur = normalize_category(default_category)
    for i, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        m = re.fullmatch(r"\[(.+?)\]", line)
        if m:
            cur = normalize_category(m.group(1))
            continue
        # 只有「已知类型名 + 分隔符」才当类型前缀，避免误切正常词条
        m = re.match(r"^([A-Za-z\u4e00-\u9fa5]{1,12})\s*[:：|,]\s*(.+)$", line)
        if m and normalize_category(m.group(1)) != "other":
            cur, pat = normalize_category(m.group(1)), m.group(2)
        else:
            pat = line
        pool.add(pat, cur, mode, f"第 {i} 行：")
    if not pool.literal and not pool.regex:
        pool._note_error("没有解析到任何规则，请检查文件内容")
    res = pool.result({})
    res["match"] = mode
    return res


def parse_csv_lexicon(text: str, default_category: str = "other", mode: str = MATCH_AUTO) -> dict:
    """解析 CSV：每行 ``类型,规则``（首行可以是表头）。"""
    pool = _Pool()
    for i, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[,\t]", line, maxsplit=1)]
        if len(parts) == 1:
            cat, pat = default_category, parts[0]
        else:
            cat, pat = parts[0], parts[1]
        if i == 1 and cat.lower() in ("category", "类型", "分类") and pat.lower() in ("pattern", "规则", "正则"):
            continue
        pool.add(pat, cat, mode, f"第 {i} 行：")
    if not pool.literal and not pool.regex:
        pool._note_error("没有解析到任何规则，请检查文件内容")
    res = pool.result({})
    res["match"] = mode
    return res


def detect_format(filename: str, text: str = "") -> str:
    ext = os.path.splitext(str(filename or ""))[1].lower().lstrip(".")
    if ext in ("json", "txt", "csv"):
        return ext
    s = str(text or "").lstrip()
    if s.startswith("{") or s.startswith("["):
        return "json"
    if "," in ((str(text or "").splitlines() or [""])[0]):
        return "csv"
    return "txt"


def parse_lexicon(text: str, fmt: str = "txt", default_category: str = "other",
                  mode: str = MATCH_AUTO) -> dict:
    f = (fmt or "txt").lower()
    if f == "json":
        return parse_json_lexicon(text)
    if f == "csv":
        return parse_csv_lexicon(text, default_category, mode)
    return parse_txt_lexicon(text, default_category, mode)


# ---------------------------------------------------------------------------
# 词库管理器
# ---------------------------------------------------------------------------

def _restrict(path: str, mode: int) -> None:
    """尽力把文件/目录权限收紧到指定值（Linux/macOS 生效，Windows 静默跳过）。"""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class LexiconManager:
    """内置 + 自定义词库的加载、启用、上传、白名单与命中测试。"""

    def __init__(self, data_dir: str, plugin_dir: Optional[str] = None, logger=None):
        self.data_dir = data_dir
        self.logger = logger
        self.plugin_dir = plugin_dir or os.path.dirname(os.path.abspath(__file__))
        self.builtin_dir = os.path.join(self.plugin_dir, BUILTIN_DIRNAME)
        self.custom_dir = os.path.join(data_dir, BUILTIN_DIRNAME)
        self.state_path = os.path.join(data_dir, STATE_FILENAME)
        os.makedirs(self.custom_dir, exist_ok=True)
        # 词库是审核用的黑名单数据，只在本地使用，不给同机其他用户读的余地
        _restrict(self.custom_dir, 0o700)
        _restrict(self.state_path, 0o600)
        self._cache: dict[str, dict] = {}          # id -> 解析结果（mtime/size 失效）
        self._compiled: tuple = ("", LiteralIndex(), [], frozenset())  # (签名, 字面量索引, 正则规则, 白名单)
        self._extra_cache: dict[tuple, list] = {}  # 群自定义关键词的编译缓存
        self.state: dict[str, Any] = self._load_state()

    # ---------------- 状态 ----------------

    def _load_state(self) -> dict:
        st: dict[str, Any] = {"version": 1, "enabled": [], "disabled": [], "allow": []}
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    st["enabled"] = [str(x) for x in (raw.get("enabled") or [])]
                    st["disabled"] = [str(x) for x in (raw.get("disabled") or [])]
                    st["allow"] = [str(x) for x in (raw.get("allow") or [])][:MAX_PATTERNS]
                    st["updated_at"] = raw.get("updated_at")
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[词库] 读取启用状态失败，按默认处理: {e}")
        if not st.get("updated_at"):
            # 首次运行：内置词库按各自的 default_enabled 决定，没写的一律开
            st["enabled"], st["disabled"] = [], []
            for path in self._list_files(self.builtin_dir):
                lid = f"builtin-{slugify(self._stem(path))}"
                (st["enabled"] if self._default_enabled(path) else st["disabled"]).append(lid)
            st["enabled"] = sorted(set(st["enabled"]))
            st["disabled"] = sorted(set(st["disabled"]))
            st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save_state(st)
        return st

    @staticmethod
    def _peek_default_enabled(path: str) -> Optional[bool]:
        """只看 JSON 头部的 default_enabled，用于首次运行决定开关。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                head = f.read(4096)
        except OSError:
            return None
        m = re.search(r'"default_enabled"\s*:\s*(true|false)', head)
        return None if not m else (m.group(1) == "true")

    def _default_enabled(self, path: str) -> bool:
        v = self._peek_default_enabled(path)
        return True if v is None else v

    def _save_state(self, st: Optional[dict] = None) -> None:
        st = st if st is not None else self.state
        st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
            _restrict(self.state_path, 0o600)
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[词库] 保存启用状态失败: {e}")

    @staticmethod
    def _list_files(d: str) -> list[str]:
        if not os.path.isdir(d):
            return []
        out = []
        for fn in sorted(os.listdir(d)):
            if fn.startswith(".") or fn.startswith("_") or fn.endswith(".tmp"):
                continue
            if os.path.splitext(fn)[1].lower() in (".json", ".txt", ".csv"):
                out.append(os.path.join(d, fn))
        return out

    @staticmethod
    def _stem(path: str) -> str:
        return os.path.splitext(os.path.basename(path))[0]

    # ---------------- 扫描 / 缓存 ----------------

    def _entry_from_file(self, path: str, source: str) -> Optional[dict]:
        try:
            stt = os.stat(path)
        except OSError:
            return None
        lid = f"{source}-{slugify(self._stem(path))}"
        cached = self._cache.get(lid)
        if cached and cached.get("mtime") == stt.st_mtime and cached.get("size") == stt.st_size:
            return cached
        base = {"id": lid, "name": self._stem(path), "source": source, "path": path,
                "categories": {}, "literals": {}, "regexes": {}, "count": 0, "cat_counts": {},
                "errors": [], "warnings": [], "error_count": 0, "warning_count": 0,
                "description": "", "version": "", "author": "", "tags": [], "sources": [],
                "kind": "rules", "match": MATCH_AUTO, "default_enabled": None,
                "notice": "", "mtime": stt.st_mtime, "size": stt.st_size}
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read(MAX_FILE_BYTES + 64)
        except Exception as e:
            base["errors"] = [f"读取失败：{e}"]
            base["error_count"] = 1
            self._cache[lid] = base
            return base
        fmt = detect_format(path, text)
        parsed = parse_lexicon(text, fmt)
        meta = parsed.get("meta") or {}
        base.update({
            "name": (meta.get("name") or self._stem(path))[:MAX_NAME_LEN],
            "format": fmt,
            "description": meta.get("description") or "",
            "version": meta.get("version") or "",
            "author": meta.get("author") or ("插件内置" if source == "builtin" else "页面上传"),
            "tags": meta.get("tags") or [],
            "sources": meta.get("sources") or [],
            "notice": meta.get("notice") or "",
            "kind": parsed.get("kind") or "rules",
            "match": parsed.get("match") or MATCH_AUTO,
            "default_enabled": meta.get("default_enabled"),
            "categories": parsed["categories"],
            "literals": parsed["literals"],
            "regexes": parsed["regexes"],
            "cat_counts": {c: len(v) for c, v in parsed["categories"].items()},
            "count": parsed["count"],
            "literal_count": parsed.get("literal_count", 0),
            "regex_count": parsed.get("regex_count", 0),
            "errors": parsed["errors"],
            "error_count": parsed.get("error_count", len(parsed["errors"])),
            "warnings": parsed["warnings"],
            "warning_count": parsed.get("warning_count", len(parsed["warnings"])),
        })
        if len(text) <= RAW_KEEP_BYTES:
            base["raw"] = text
        self._cache[lid] = base
        return base

    def scan(self, force: bool = False) -> list[dict]:
        if force:
            self._cache.clear()
            self._compiled = ("", LiteralIndex(), [], frozenset())
            self._extra_cache.clear()
        out: list[dict] = []
        for path in self._list_files(self.builtin_dir):
            e = self._entry_from_file(path, "builtin")
            if e:
                out.append(e)
        for path in self._list_files(self.custom_dir):
            e = self._entry_from_file(path, "custom")
            if e:
                out.append(e)
        out.sort(key=lambda x: (0 if x["source"] == "builtin" else 1, x["name"]))
        return out

    def get(self, lid: str, force: bool = False) -> Optional[dict]:
        for e in self.scan(force=force):
            if e["id"] == lid:
                return e
        return None

    # ---------------- 启用状态 ----------------

    def is_enabled(self, entry: dict) -> bool:
        lid = entry["id"]
        if lid in self.state.get("enabled", []):
            return True
        if lid in self.state.get("disabled", []):
            return False
        # 没记录过：按文件里的 default_enabled，没写就内置开、自定义关
        if entry.get("default_enabled") is not None:
            return bool(entry["default_enabled"])
        return entry.get("source") == "builtin"

    def set_enabled(self, lid: str, enabled: bool) -> dict:
        en = [x for x in self.state.get("enabled", []) if x != lid]
        dis = [x for x in self.state.get("disabled", []) if x != lid]
        (en if enabled else dis).append(lid)
        self.state["enabled"] = sorted(set(en))
        self.state["disabled"] = sorted(set(dis))
        self._save_state()
        self._compiled = ("", LiteralIndex(), [], frozenset())   # 让编译缓存失效
        return self.stats()

    def apply(self, ids: list[str]) -> dict:
        """一次性设定启用集合（页面上「全开 / 仅内置 / 全关」用）。"""
        valid = {e["id"] for e in self.scan()}
        want = {str(i) for i in (ids or [])} & valid
        self.state["enabled"] = sorted(want)
        self.state["disabled"] = sorted(valid - want)
        self._save_state()
        self._compiled = ("", LiteralIndex(), [], frozenset())
        return self.stats()

    # ---------------- 白名单 ----------------

    def allow_words(self) -> set[str]:
        """内置白名单词库 + 页面手动加的，合并成集合。"""
        out = {str(w).strip() for w in (self.state.get("allow") or []) if str(w).strip()}
        for e in self.scan():
            if e.get("kind") != "allow" or not self.is_enabled(e):
                continue
            for _, words in (e.get("literals") or {}).items():
                out |= {w for w in words if w}
        return out

    def allow_add(self, words: list[str] | str) -> dict:
        items = [words] if isinstance(words, str) else list(words or [])
        cur = {str(w).strip() for w in (self.state.get("allow") or []) if str(w).strip()}
        added = [w for w in (str(x).strip() for x in items) if w and w not in cur]
        cur |= set(added)
        self.state["allow"] = sorted(cur)[:MAX_PATTERNS]
        self._save_state()
        self._compiled = ("", LiteralIndex(), [], frozenset())
        return {"ok": True, "added": added, "count": len(self.state["allow"])}

    def allow_remove(self, words: list[str] | str) -> dict:
        items = [words] if isinstance(words, str) else list(words or [])
        drop = {str(x).strip() for x in items if str(x).strip()}
        cur = [w for w in (self.state.get("allow") or []) if w not in drop]
        self.state["allow"] = sorted(set(cur))
        self._save_state()
        self._compiled = ("", LiteralIndex(), [], frozenset())
        return {"ok": True, "removed": sorted(drop), "count": len(self.state["allow"])}

    def allow_clear(self) -> dict:
        self.state["allow"] = []
        self._save_state()
        self._compiled = ("", LiteralIndex(), [], frozenset())
        return {"ok": True, "count": 0}

    # ---------------- 规则池 / 命中 ----------------

    def _build_compiled(self) -> tuple[str, LiteralIndex, list[tuple], frozenset]:
        """把启用的词库编译成 (签名, 字面量索引, 正则规则表, 白名单)，词库没变就复用。"""
        entries = self.scan()
        sig = "|".join(f"{e['id']}:{e['mtime']}:{e['size']}" for e in entries if self.is_enabled(e))
        sig += "#" + ",".join(sorted(str(w) for w in self.state.get("allow") or []))
        if sig == self._compiled[0]:
            return self._compiled
        index = LiteralIndex()
        regexes: list[tuple] = []
        allow: set[str] = {str(w).strip() for w in (self.state.get("allow") or []) if str(w).strip()}
        for e in entries:
            if not self.is_enabled(e) or e.get("kind") == "allow":
                continue
            for cat, words in (e.get("literals") or {}).items():
                for w in words:
                    index.add(w, (cat, w, e["id"], e["name"]))
            for cat, pats in (e.get("regexes") or {}).items():
                for p in pats:
                    try:
                        regexes.append((re.compile(p, FLAGS), cat, p, e["id"], e["name"]))
                    except re.error:
                        continue
        for e in entries:
            if e.get("kind") != "allow" or not self.is_enabled(e):
                continue
            for _, words in (e.get("literals") or {}).items():
                allow |= {w for w in words if w}
        self._compiled = (sig, index, regexes, frozenset(allow))
        return self._compiled

    def _extra_compiled(self, extra_keywords: Optional[dict]) -> list[tuple]:
        if not extra_keywords:
            return []
        key = tuple(sorted((c, tuple(pats or [])) for c, pats in extra_keywords.items()))
        if key in self._extra_cache:
            return self._extra_cache[key]
        items: list[tuple] = []
        for cat, pats in extra_keywords.items():
            for raw in (pats or []):
                pat, is_re, err = split_pattern(str(raw), MATCH_AUTO)
                if err:
                    continue
                src = f"本群自定义（{'正则' if is_re else '词'}）"
                if is_re:
                    try:
                        items.append((re.compile(pat, FLAGS), normalize_category(cat), pat, "group", src))
                    except re.error:
                        continue
                else:
                    items.append((None, normalize_category(cat), pat, "group", src))
        self._extra_cache[key] = items
        if len(self._extra_cache) > 64:      # 简单上限，避免长期运行内存膨胀
            self._extra_cache.clear()
            self._extra_cache[key] = items
        return items

    def hits(self, text: str, extra_keywords: Optional[dict] = None,
             limit: int = 20, include_disabled: bool = False,
             use_allow: bool = True) -> list[dict]:
        """返回全部命中（用于页面命中测试），按位置排序。"""
        t = str(text or "")
        if not t:
            return []
        if include_disabled:
            _, idx, rx_items = self._build_all_index()
            allow = self.allow_words() if use_allow else set()
        else:
            _, idx, rx_items, cached_allow = self._build_compiled()
            allow = cached_allow if use_allow else set()

        raw: list[tuple] = list(idx.search(t, limit=max(limit * 4, 40), allow=allow))
        for rx, cat, pat, sid, sname in rx_items:
            try:
                m = rx.search(t)
            except Exception:
                continue
            if not m:
                continue
            if use_allow and m.group(0) in allow:
                continue
            raw.append((m.start(), m.end(), (cat, m.group(0), sid, sname), m.group(0)))

        # 群自定义关键词：字面量的走子串查找，正则的直接跑
        for rx, cat, pat, sid, sname in self._extra_compiled(extra_keywords):
            if rx is not None:
                try:
                    m = rx.search(t)
                except Exception:
                    continue
                if m and not (use_allow and m.group(0) in allow):
                    raw.append((m.start(), m.end(), (cat, m.group(0), sid, sname), m.group(0)))
            else:
                start = t.lower().find(pat.lower())
                if start >= 0 and not (use_allow and pat in allow):
                    raw.append((start, start + len(pat), (cat, pat, sid, sname), pat))

        raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        out: list[dict] = []
        seen: set[tuple] = set()
        for start, end, info, matched in raw:
            cat, pattern, sid, sname = info
            key = (cat, matched.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"category": cat, "pattern": pattern, "source_id": sid, "source": sname,
                        "match": matched[:80], "span": [start, end]})
            if len(out) >= limit:
                break
        return out

    def _build_all_index(self) -> tuple[str, LiteralIndex, list[tuple]]:
        """命中测试用：把所有词库（含停用的）编译出来。"""
        index = LiteralIndex()
        regexes: list[tuple] = []
        for e in self.scan():
            if e.get("kind") == "allow":
                continue
            for cat, words in (e.get("literals") or {}).items():
                for w in words:
                    index.add(w, (cat, w, e["id"], f"{e['name']}{'' if self.is_enabled(e) else '（已停用）'}"))
            for cat, pats in (e.get("regexes") or {}).items():
                for p in pats:
                    try:
                        regexes.append((re.compile(p, FLAGS), cat, p, e["id"], e["name"]))
                    except re.error:
                        continue
        return ("all", index, regexes)

    def hit(self, text: str, extra_keywords: Optional[dict] = None) -> Optional[dict]:
        """返回第一条命中（判定链路用，要求最快）。"""
        h = self.hits(text, extra_keywords=extra_keywords, limit=1)
        return h[0] if h else None

    def literal_words(self) -> list[tuple[str, tuple]]:
        """导出启用词库的全部字面量词条 (词, 元信息)，供变体索引（拼音/形近）复用。"""
        try:
            _, idx, _, _ = self._build_compiled()
            return idx.items()
        except Exception:
            return []

    def pool(self, extra_keywords: Optional[dict] = None) -> dict[str, list[str]]:
        """合并后的规则池 {类型: [规则文本]}（只看启用的库），供其他模块复用。"""
        out: dict[str, list[str]] = {}
        for e in self.scan():
            if not self.is_enabled(e) or e.get("kind") == "allow":
                continue
            for cat, pats in (e.get("categories") or {}).items():
                out.setdefault(cat, [])
                for p in pats:
                    if p not in out[cat]:
                        out[cat].append(p)
        for cat, pats in (extra_keywords or {}).items():
            c = normalize_category(cat)
            out.setdefault(c, [])
            for p in (pats or []):
                if p not in out[c]:
                    out[c].append(p)
        return out

    # ---------------- 增删改 ----------------

    def upload(self, name: str, content: str, fmt: str = "", default_category: str = "other",
               description: str = "", mode: str = "new", target_id: str = "",
               match_mode: str = MATCH_AUTO) -> dict:
        """上传/粘贴一份词库。

        mode: new（新建）/ overwrite（覆盖同名或指定 id）/ append（追加到指定 id）
        """
        if not str(content or "").strip():
            return {"ok": False, "error": "内容是空的"}
        raw_bytes = len(str(content).encode("utf-8"))
        if raw_bytes > MAX_FILE_BYTES:
            return {"ok": False, "error": f"文件过大（>{MAX_FILE_BYTES // 1024} KB）"}

        parsed = parse_lexicon(content, fmt or detect_format(name, content),
                               default_category, match_mode)
        cats: dict[str, list[str]] = parsed["categories"]
        if not cats:
            return {"ok": False, "error": "没有解析到任何规则", "errors": parsed["errors"]}
        if sum(len(v) for v in cats.values()) > MAX_PATTERNS:
            return {"ok": False, "error": f"规则条数超过上限（{MAX_PATTERNS} 条）"}

        display = (str(name or "").strip() or "未命名词库")[:MAX_NAME_LEN]
        mode = (mode or "new").lower()
        kind = parsed.get("kind") or "rules"

        if mode == "append" and target_id:
            cur = self.get(target_id)
            if not cur:
                return {"ok": False, "error": "要追加的词库不存在"}
            if cur["source"] == "builtin":
                return {"ok": False, "error": "内置词库不可修改，请「另存为新词库」"}
            pool = {c: list(v) for c, v in cur["categories"].items()}
            added = 0
            for c, pats in cats.items():
                pool.setdefault(c, [])
                for p in pats:
                    if p not in pool[c]:
                        pool[c].append(p)
                        added += 1
            self._write_custom(target_id, pool, cur["name"],
                               description or cur.get("description", ""), cur.get("version", ""),
                               kind=cur.get("kind", "rules"), match=cur.get("match", MATCH_AUTO))
            return {"ok": True, "id": target_id, "mode": "append", "added": added,
                    "count": sum(len(v) for v in pool.values()), "name": cur["name"],
                    "errors": parsed["errors"], "warnings": parsed["warnings"]}

        lid = f"custom-{slugify(display)}"
        if mode == "overwrite" and target_id:
            cur = self.get(target_id)
            if cur and cur["source"] == "builtin":
                return {"ok": False, "error": "内置词库不可覆盖，请换个名字新建"}
            lid = target_id
        exists = self.get(lid)
        if exists and mode != "overwrite":
            return {"ok": False, "error": f"已存在同名词库「{exists['name']}」，请改名或选择覆盖",
                    "exists_id": lid}

        self._write_custom(lid, cats, display, description,
                           str(parsed.get("meta", {}).get("version") or ""),
                           kind=kind, regexes=parsed.get("regexes") or {}, match=parsed.get("match", MATCH_AUTO))
        # 新建的自定义词库默认启用：用户既然传了，多半就是想马上用（页面上可一键关掉）
        if not (mode == "overwrite" and lid in self.state.get("enabled", [])):
            self.set_enabled(lid, True)
        else:
            self._save_state()
        return {"ok": True, "id": lid, "mode": mode, "count": sum(len(v) for v in cats.values()),
                "name": display, "kind": kind, "errors": parsed["errors"], "warnings": parsed["warnings"]}

    def _write_custom(self, lid: str, cats: dict, name: str, description: str = "",
                      version: str = "", kind: str = "rules", regexes: Optional[dict] = None,
                      match: str = MATCH_AUTO) -> str:
        slug = lid.split("-", 1)[1] if lid.startswith("custom-") else slugify(name)
        path = os.path.join(self.custom_dir, f"{slug}.json")
        if regexes:
            # 正则单独放 regexes 区，categories 里只留字面量词，
            # 否则导出再导入时正则会被 auto 模式重复解析一遍
            cats = {c: [x for x in v if x not in set(regexes.get(c) or [])] for c, v in cats.items()}
        payload: dict[str, Any] = {
            "name": name[:MAX_NAME_LEN],
            "description": (description or "")[:300],
            "version": version or time.strftime("%Y.%m%d"),
            "author": "页面上传",
            "match": match if match in (MATCH_AUTO, MATCH_LITERAL, MATCH_REGEX) else MATCH_AUTO,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "categories": {c: sorted(set(v)) for c, v in cats.items() if v},
        }
        if kind == "allow":
            payload["kind"] = "allow"
        if regexes:
            payload["regexes"] = {c: sorted(set(v)) for c, v in regexes.items() if v}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        _restrict(path, 0o600)
        self._cache.pop(lid, None)
        self._compiled = ("", LiteralIndex(), [], frozenset())
        return path

    def delete(self, lid: str) -> dict:
        e = self.get(lid)
        if not e:
            return {"ok": False, "error": "词库不存在"}
        if e["source"] == "builtin":
            return {"ok": False, "error": "内置词库不可删除，可以停用"}
        try:
            os.remove(e["path"])
        except Exception as ex:
            return {"ok": False, "error": f"删除失败：{ex}"}
        self._cache.pop(lid, None)
        self.state["enabled"] = [x for x in self.state.get("enabled", []) if x != lid]
        self.state["disabled"] = [x for x in self.state.get("disabled", []) if x != lid]
        self._save_state()
        self._compiled = ("", LiteralIndex(), [], frozenset())
        return {"ok": True, "id": lid}

    def rename(self, lid: str, name: str) -> dict:
        e = self.get(lid)
        if not e:
            return {"ok": False, "error": "词库不存在"}
        if e["source"] == "builtin":
            return {"ok": False, "error": "内置词库名不可改"}
        nm = str(name or "").strip()[:MAX_NAME_LEN]
        if not nm:
            return {"ok": False, "error": "名称不能为空"}
        self._write_custom(lid, e["categories"], nm, e.get("description", ""), e.get("version", ""),
                           kind=e.get("kind", "rules"), regexes=e.get("regexes") or {},
                           match=e.get("match", MATCH_AUTO))
        return {"ok": True, "id": lid, "name": nm}

    def export(self, lid: str) -> dict:
        e = self.get(lid)
        if not e:
            return {"ok": False, "error": "词库不存在"}
        payload: dict[str, Any] = {
            "name": e["name"],
            "description": e.get("description", ""),
            "version": e.get("version", ""),
            "author": e.get("author", ""),
            "match": e.get("match", MATCH_AUTO),
            "categories": e["categories"] if not e.get("regexes") else e["literals"],
        }
        if e.get("regexes"):
            payload["regexes"] = e["regexes"]
        if e.get("kind") == "allow":
            payload["kind"] = "allow"
        if e.get("notice"):
            payload["notice"] = e["notice"]
        return {"ok": True, "id": lid, "filename": f"{slugify(e['name'])}.json",
                "content": json.dumps(payload, ensure_ascii=False, indent=1)}

    # ---------------- 汇总 ----------------

    def stats(self) -> dict:
        entries = self.scan()
        enabled = [e for e in entries if self.is_enabled(e)]
        return {
            "lexicon_count": len(entries),
            "enabled_count": len(enabled),
            "builtin_count": sum(1 for e in entries if e["source"] == "builtin"),
            "custom_count": sum(1 for e in entries if e["source"] == "custom"),
            "pattern_count": sum(e["count"] for e in enabled),
            "literal_count": sum(int(e.get("literal_count") or 0) for e in enabled),
            "regex_count": sum(int(e.get("regex_count") or 0) for e in enabled),
            "total_pattern_count": sum(e["count"] for e in entries),
            "error_count": sum(e.get("error_count", 0) for e in entries),
            "warning_count": sum(e.get("warning_count", 0) for e in entries),
            "allow_count": len(self.allow_words()),
            "manual_allow_count": len(self.state.get("allow") or []),
            "enabled_ids": sorted(e["id"] for e in enabled),
            "group_extra": "本群自定义关键词仍然生效，与词库合并去重后一起判定",
        }

    def list_lexicons(self, with_patterns: bool = False, preview: int = MAX_PREVIEW) -> list[dict]:
        out = []
        for e in self.scan():
            item = {k: v for k, v in e.items() if k not in ("raw", "literals", "regexes")}
            item["enabled"] = self.is_enabled(e)
            if with_patterns:
                item["patterns"] = {c: v[:preview] for c, v in (e.get("categories") or {}).items()}
                item["preview_limit"] = preview
            item["literal_count"] = int(e.get("literal_count") or 0)
            item["regex_count"] = int(e.get("regex_count") or 0)
            out.append(item)
        return out


def self_test() -> None:  # pragma: no cover - 手动跑
    import tempfile
    d = tempfile.mkdtemp()
    mgr = LexiconManager(d, plugin_dir=os.path.dirname(os.path.abspath(__file__)))
    st = mgr.stats()
    print(json.dumps({k: st[k] for k in ("lexicon_count", "enabled_count", "pattern_count",
                                         "literal_count", "allow_count")}, ensure_ascii=False))
    t0 = time.time()
    mgr._build_compiled()
    print("首次编译耗时 %.3fs" % (time.time() - t0))
    for s in ["加我vx：abc123 进群领福利", "今天天气不错，晚上一起看电影啊",
              "扫码进群领红包", "the quick brown fox"]:
        t1 = time.time()
        h = mgr.hit(s)
        print(f"{(time.time() - t1) * 1000:7.2f}ms  {s} -> {h}")


if __name__ == "__main__":  # pragma: no cover
    self_test()
