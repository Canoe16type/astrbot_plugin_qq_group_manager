"""群规审核核心（四维度判定 + 话术渲染 + 判定缓存 + 记录存储）。

设计要点（与用户需求一一对应）：
1. **只在被举报时生效**：本模块不主动扫描消息，只有 main.py 收到 `/举报` 才调用；
2. **四维度判定**：keyword（关键词规则库）/ rule（本群群规条目）/
   semantic（文本模型 + 上下文语义）/ vision（多模态识别图片）；
   返回时把命中的维度作为话术的「根据{basis}」；
3. **时长不由 AI 决定**：AI 只输出 basis/category/severity/confidence，
   时长由本模块的规则表映射，并受 max_mute_seconds 上限约束；
4. **缓存命中率**：system_prompt 只放稳定内容（角色/分类/JSON 格式/few-shot/
   通用禁止项/本群群规），动态消息放 user prompt —— 前缀稳定才吃得到前缀缓存。

本模块不 import AstrBot，纯逻辑 + JSON 存储，方便离线单测。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量：维度 / 分类 / 默认话术
# ---------------------------------------------------------------------------

#: 四个判定维度。命中的维度会作为话术里的「根据{basis}」回填。
DIM_KEYWORD = "keyword"
DIM_RULE = "rule"
DIM_SEMANTIC = "semantic"
DIM_VISION = "vision"
ALL_DIMS = (DIM_KEYWORD, DIM_RULE, DIM_SEMANTIC, DIM_VISION)

#: 维度默认显示名（可在插件 Page 里逐群/全局改）
DEFAULT_DIM_NAMES: dict[str, str] = {
    DIM_KEYWORD: "关键词",
    DIM_RULE: "群规",
    DIM_SEMANTIC: "语义分析",
    DIM_VISION: "图片识别",
}

#: 违规类型默认显示名（可在 Page 里改）
DEFAULT_CAT_NAMES: dict[str, str] = {
    "ad": "广告引流",
    "abuse": "人身攻击",
    "porn": "色情低俗",
    "illegal": "违法违规",
    "spam": "刷屏灌水",
    "other": "其他违规",
}

#: 严重度 -> 默认禁言秒数（未命中具体规则时使用）
DEFAULT_SEVERITY_SECONDS: dict[str, int] = {
    "low": 600,      # 10 分钟
    "medium": 3600,  # 1 小时
    "high": 86400,   # 1 天
}

#: 变体累计升级（variant strike）档位。空串 = 本次未触发升级。
STRIKE_NONE = ""
#: 达到提醒阈值：该条记录进待办并高亮，按设置决定是否被动消息 @ 已登记管理员
STRIKE_TODO = "todo"
#: 达到禁言阈值（需群管自行开启）：自动禁言一次，带冷却
STRIKE_MUTE = "mute"

#: 人工标注过的「误判」feedback 取值：统计前科 / 变体累计时一律排除
FALSE_POSITIVE_FEEDBACK = ("false_positive", "mistake", "误判")

#: 变体累计升级相关设置键（面板渲染、后端校验共用一份，避免两边漏改）
VARIANT_STRIKE_KEYS = (
    "variant_strike_enabled",
    "variant_strike_window_days",
    "variant_strike_threshold",
    "variant_strike_notify",
    "variant_strike_mute",
    "variant_strike_mute_threshold",
    "variant_strike_mute_seconds",
    "variant_strike_require_confirm",
    "variant_strike_cooldown_hours",
)


def is_false_positive(v: dict) -> bool:
    """该记录是否被人工标过误判。所有派生统计（前科 / 变体累计）都要排除它。"""
    return str((v or {}).get("feedback") or "") in FALSE_POSITIVE_FEEDBACK


def is_strike_muted(v: dict) -> bool:
    """该条变体记录是否触发了「累计升级自动禁言」。

    唯一的写入点是 _mod_variant_strike 落盘的 variant_strike.muted，所以这里以它为准；
    同时兼容早期/外部写入的顶层 variant_strike_muted（老记录不会丢计数）。
    """
    v = v or {}
    if (v.get("variant_strike") or {}).get("muted"):
        return True
    return bool(v.get("variant_strike_muted"))

#: 默认话术（与用户指定的两条一致，第三条为「综合判定违规但无对应规则」兜底）
#: 注意：插件没有「人工审核」环节，话术不得承诺人工处理；记录位置=面板「记录 → 判定记录」
DEFAULT_TEMPLATES: dict[str, str] = {
    "mod_hit": "根据{basis}，符合{category}，已被禁言{duration}",
    "mod_clean": "根据多重维度评判，此消息不涉嫌违规",
    "mod_hit_general": "根据多重维度评判，符合{category}，已被禁言{duration}",
    "mod_receiving": "已收到举报，正在审核中…",
    "mod_failed": "审核未完成（{error}），已记入审核记录",
    "mod_no_quote": "请先长按违规消息选择「回复」引用它，再发送 /举报",
    "mod_quote_lost": "没找到你引用的那条消息（可能太旧或已撤回），麻烦重新引用违规消息后再发一次 /举报",
    "mod_layer_off": "本群已关闭模型判定（仅关键词层），本次未命中词库，未做语义判定",
    "mod_disabled": "本群未开启举报审核",
    "mod_dry_run": "（试运行）根据{basis}，符合{category}，本应禁言{duration}",
    "mod_need_admin": "只有管理员可以修改群规",
    "mod_rules_empty": "本群还没有配置群规。管理员可用：/群规 添加 规则名 | 判定说明 | 时长",
    "mod_report_limit": "举报太频繁啦，请稍后再试。",
    "mod_uncertain": "判定把握不足，本次未自动处理，已记入审核记录",
    "mod_exempt": "该成员在豁免名单中，本次不做处理",
    "mod_variant_recalled": "该消息疑似用变体写法绕过检测（{method}），已撤回并记录，请管理员复核",
    "mod_variant_logged": "该消息疑似用变体写法绕过检测（{method}），已记录待复核",
    "mod_todo_muted": "已按待办处理：禁言 {user} {duration}",
    # ---- 变体累计升级（strike）----
    "mod_variant_strike_notify": "{mentions} 疑似变体累计提醒：{user} 在近 {days} 天内已有 {count} 次疑似变体写法（本次：{method}）。已进待办，请到插件页面复核处理。",
    "mod_variant_strike_muted": "{mentions} 疑似变体累计已达 {count} 次（近 {days} 天），已自动禁言 {user} {duration}。若判断有误，请解禁并在插件页面标记误判（标记后不再计入累计）。",
    # ---- 管理员登记（变体提醒名单）----
    "mod_admin_registered": "{user} 登记成功，已加入本群审核提醒名单（当前 {count} 人）。变体累计提醒会 @ 名单内成员。",
    "mod_admin_unregistered": "{user} 已从本群审核提醒名单移除（当前 {count} 人）。",
    "mod_admin_not_registered": "{user} 本来就不在审核提醒名单里（当前 {count} 人）。",
    "mod_admin_list": "本群审核提醒名单（{count} 人）：{admins}",
    "mod_admin_list_empty": "本群还没有人登记为审核管理员。管理员可发送「/管理登记」把自己加入提醒名单。",
    "mod_admin_register_off": "提示：本群未开启「变体累计提醒」，登记已保存，但暂时不会收到 @（可在插件页面「举报设置」里打开）。",
}

#: 内置关键词库（命中即判「根据关键词」，不调模型；可在 Page 里改）
DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "ad": [
        r"加\s*(?:我|微信|vx|VX|v信|威信|QQ|qq)",
        r"(?:微信|vx|VX|qq|QQ)\s*[:：]\s*[0-9A-Za-z_-]{5,}",
        r"(?:刷单|返利|兼职日结|打字员|代刷|薅羊毛群)",
        r"(?:扫码|扫我|扫下面).{0,6}(?:领|进群|加)",
        r"(?:推广|引流|广告位|投放).{0,6}(?:联系|私聊)",
    ],
    "illegal": [
        r"(?:出售|收购|代办).{0,6}(?:实名|账号|身份证|银行卡|发票)",
        r"(?:赌博|博彩|六合彩|下注|洗钱)",
    ],
    "porn": [
        r"(?:色情|裸聊|福利姬|约炮|招嫖)",
    ],
    "spam": [
        r"^(.)\1{9,}$",
    ],
}

#: 判定结果缓存 TTL（秒）
DEFAULT_CACHE_TTL = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def norm_text(text: str) -> str:
    """归一化文本用于哈希缓存：去空白/换行、统一小写、压缩重复字符。"""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"(.)\1{3,}", r"\1\1", t)
    return t


def text_hash(text: str) -> str:
    return hashlib.sha256(norm_text(text).encode("utf-8")).hexdigest()[:32]


def parse_json_loose(raw: str) -> Optional[dict]:
    """从模型输出里宽松提取第一个 JSON 对象（容忍 ```json 包裹与首尾废话）。"""
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?", "", s, flags=re.I).strip()
    s = re.sub(r"```$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 退化为括号配对扫描
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


def render_tpl(tpl: str, **kw: Any) -> str:
    """安全渲染话术：只替换白名单变量，缺值用空串，绝不抛异常。"""
    out = str(tpl or "")
    for k, v in kw.items():
        out = out.replace("{" + k + "}", "" if v is None else str(v))
    # 未提供的变量残留 {} 时清掉，避免出现「根据《》，符合违规」
    out = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", "", out)
    return out


def keyword_hit(text: str, extra_keywords: Optional[dict[str, list[str]]] = None) -> Optional[dict]:
    """关键词维度：返回 {"category":..., "pattern":..., "rule_name":...} 或 None。

    extra_keywords 为群自定义关键词（在 Page 里配），与内置库合并判断。
    """
    if not text:
        return None
    pools: dict[str, list[str]] = {k: list(v) for k, v in DEFAULT_KEYWORDS.items()}
    for cat, pats in (extra_keywords or {}).items():
        pools.setdefault(cat, []).extend(pats or [])
    for cat, pats in pools.items():
        for pat in pats:
            try:
                if re.search(pat, text, flags=re.I | re.M):
                    return {"category": cat, "pattern": pat}
            except re.error:
                continue
    return None


def severity_to_seconds(severity: str, table: Optional[dict[str, int]] = None) -> int:
    t = table or DEFAULT_SEVERITY_SECONDS
    return int(t.get(str(severity or "").lower(), t.get("medium", 3600)))


def decide_action(
    verdict: dict,
    *,
    rules: list[dict],
    max_seconds: int = 86400,
    default_seconds: int = 600,
) -> dict:
    """把 AI（或关键词层）的判定翻译成可执行动作 + 时长。

    规则：命中具体群规 -> 用该规则配置的时长；否则按 severity 查表；
    最后统一 clamp 到 [60, max_seconds]。AI 无权直接指定秒数。
    """
    if not verdict or not verdict.get("violation"):
        return {"action": "none", "seconds": 0, "basis": verdict.get("basis") if verdict else None}

    seconds: Optional[int] = None
    matched_rule = None
    usable = [r for r in (rules or []) if r.get("enabled", True)]

    def _take(rule: Optional[dict]) -> Optional[int]:
        if not rule:
            return None
        try:
            v = int(rule.get("seconds") or 0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    rid = str(verdict.get("matched_rule_id") or "").strip()
    if rid:
        matched_rule = next((r for r in usable if str(r.get("id")) == rid), None)
    if matched_rule is None:
        # 模型常只回规则“名字”，按名称匹配（双向包含，容忍措辞差异）
        rn = str(verdict.get("rule_name") or "").strip()
        if rn:
            matched_rule = next(
                (r for r in usable if str(r.get("name") or "").strip()
                 and (str(r["name"]).strip() in rn or rn in str(r["name"]).strip())),
                None,
            )
    if matched_rule is None:
        # 兜底：按违规类型找同类规则（关键词层也适用，保证群规配置的时长优先）
        cat = str(verdict.get("category") or "")
        if cat:
            same = [r for r in usable if str(r.get("category") or "") == cat]
            if len(same) == 1:
                matched_rule = same[0]
            elif same:
                sev = str(verdict.get("severity") or "")
                matched_rule = next((r for r in same if str(r.get("severity") or "") == sev), None)
    if matched_rule is not None:
        seconds = _take(matched_rule)
    if seconds is None:
        seconds = severity_to_seconds(str(verdict.get("severity") or "medium"))
    if seconds < 60:
        seconds = default_seconds
    seconds = max(60, min(int(seconds), int(max_seconds)))

    return {
        "action": "mute",
        "seconds": seconds,
        "rule_name": (matched_rule or {}).get("name") or "",
        "basis": verdict.get("basis") or DIM_SEMANTIC,
    }


def escalate_seconds(
    seconds: int,
    prior_offenses: int,
    *,
    step: float = 1.0,
    max_multiplier: float = 4.0,
    cap: int = 86400,
) -> tuple[int, float]:
    """复犯累进：按前科次数把时长乘上一个倍率。

    倍率 = min(1 + 前科次数 * step, max_multiplier)，step=1.0 即「第二次翻倍、第三次三倍」。
    只做乘法与截断，不改判定结果——**能不能累进由调用方（群配置）决定**。
    返回 (时长, 倍率)；前科为 0 或参数异常时原样返回。
    """
    base = int(seconds or 0)
    try:
        n = max(0, int(prior_offenses or 0))
        st = float(step if step is not None else 1.0)
        mx = float(max_multiplier if max_multiplier is not None else 1.0)
    except (TypeError, ValueError):
        return base, 1.0
    if n <= 0 or st <= 0 or mx <= 1.0:
        return base, 1.0
    mult = min(1.0 + n * st, max(mx, 1.0))
    out = int(round(base * mult))
    out = max(base, min(out, int(cap or 86400), 30 * 86400))
    return out, round(out / base, 2) if base else 1.0


# ---------------------------------------------------------------------------
# Prompt 构建（前缀缓存友好：稳定内容在 system，动态内容在 user）
# ---------------------------------------------------------------------------

ROLE_AND_RULES = """你是一个 QQ 群聊内容审核员。你只判断「被举报的这条消息」是否违反本群群规。

判定原则：
1. 只依据被举报消息本身 + 给出的上下文判断，不臆测、不扩大解释；
2. 举报人附言仅作参考，不得作为违规依据（举报可能与对方存在私人矛盾）；
3. 反讽、玩笑、引用他人言论、正常讨论不判违规；确实无法判断时判不违规；
4. 宁可漏判不可错判：置信度低于要求的判定请给 violation=false。

判定维度 basis 只能取以下四个值之一（取最贴近的那个）：
- keyword：命中明确的关键词/正则黑名单（如联系方式、引流话术）
- rule：命中本群群规中的某一条
- semantic：结合语义与上下文对话判定（如人身攻击、阴阳怪气、刷屏）
- vision：依据图片内容判定（如二维码、涉黄涉暴图片）

输出要求：只输出一个 JSON 对象，不要任何解释、前后缀或代码块。
JSON 字段：
{
  "violation": true/false,
  "basis": "keyword|rule|semantic|vision",
  "matched_rule_id": "命中的群规条目 id，没有则为 null",
  "category": "违规类型英文 key，见下方分类表",
  "severity": "low|medium|high",
  "confidence": 0.0~1.0,
  "reason": "一句话说明判定理由",
  "evidence": "原文中最能说明问题的片段，没有则为空串"
}"""


def build_system_prompt(
    *,
    dim_names: Optional[dict[str, str]] = None,
    cat_names: Optional[dict[str, str]] = None,
    group_rules: Optional[list[dict]] = None,
    few_shot: str = "",
    common_ban: str = "",
) -> str:
    """构建**稳定前缀**：不含时间戳/群号/消息 ID，保证跨请求命中前缀缓存。

    顺序：角色原则 -> 分类表 -> few-shot -> 通用禁止项 -> 本群群规（最后，逐群变化）
    """
    dims = dict(DEFAULT_DIM_NAMES)
    dims.update(dim_names or {})
    cats = dict(DEFAULT_CAT_NAMES)
    cats.update(cat_names or {})

    parts = [ROLE_AND_RULES]
    parts.append("\n违规类型（category 必须取下列 key 之一）：")
    for k, v in cats.items():
        parts.append(f"- {k}：{v}")
    parts.append(
        "\n维度显示名（仅供你理解 basis 含义，输出仍用英文 key）："
        + "；".join(f"{k}={v}" for k, v in dims.items())
    )

    if few_shot.strip():
        parts.append("\n判定示例（务必参照其尺度）：\n" + few_shot.strip())

    if common_ban.strip():
        parts.append("\n通用禁止项（所有群都适用）：\n" + common_ban.strip())

    # ---- 以下是逐群变化的部分，放最后，尽量不污染前面的共享前缀 ----
    if group_rules:
        lines = ["\n本群群规（判定时优先对照，命中请回填 matched_rule_id）："]
        for r in group_rules:
            if not r.get("enabled", True):
                continue
            lines.append(
                f"- id={r.get('id')} 规则名={r.get('name')} "
                f"说明={r.get('desc')} 类型={r.get('category')} "
                f"严重度={r.get('severity')}"
            )
        if len(lines) > 1:
            parts.append("\n".join(lines))
        else:
            parts.append("\n本群群规：（未配置，请仅依据通用禁止项判断，basis 用 semantic）")
    else:
        parts.append("\n本群群规：（未配置，请仅依据通用禁止项判断，basis 用 semantic）")

    return "\n".join(parts)


def build_user_prompt(
    *,
    message_text: str,
    sender_name: str = "",
    context_lines: Optional[list[str]] = None,
    reporter_note: str = "",
    has_image: bool = False,
) -> str:
    """构建**动态部分**（每次请求都变，放在 prompt 末尾）。"""
    lines = ["【待审消息】", f"发送者：{sender_name or '未知'}", f"内容：{message_text or '（无文本）'}"]
    if has_image:
        lines.append("（该消息包含图片，请结合图片内容判断）")
    if context_lines:
        lines.append("\n【上下文（由早到晚，最后一条为待审消息所在位置附近）】")
        lines.extend(context_lines)
    if reporter_note:
        lines.append("\n【举报人附言（仅参考，不可作为违规依据）】")
        lines.append(reporter_note)
    lines.append("\n请按 system 要求只输出 JSON。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 存储（JSON 落盘；群规/设置/话术按群隔离）
# ---------------------------------------------------------------------------

class ModerationStore:
    """群规、举报设置、话术、判定记录的持久化。"""

    def __init__(self, data_dir: str, logger=None):
        self.dir = data_dir
        self.logger = logger
        os.makedirs(self.dir, exist_ok=True)
        self._rules_path = os.path.join(self.dir, "mod_rules.json")
        self._settings_path = os.path.join(self.dir, "mod_settings.json")
        self._tpl_path = os.path.join(self.dir, "mod_templates.json")
        self._verdict_path = os.path.join(self.dir, "mod_verdicts.json")
        # 群名/备注 + 机器人昵称（openid 不直观，这里做本地映射）
        self._meta_path = os.path.join(self.dir, "mod_group_meta.json")
        # 审核提醒名单（管理员用「/管理登记」逐群自证登记，变体累计提醒只 @ 名单内的人）
        self._admin_path = os.path.join(self.dir, "mod_admins.json")

        self.rules: dict[str, list[dict]] = self._load(self._rules_path, {}) or {}
        self.settings: dict[str, dict] = self._load(self._settings_path, {}) or {}
        self.templates: dict[str, Any] = self._load(self._tpl_path, {}) or {}
        self.verdicts: list[dict] = self._load(self._verdict_path, []) or []
        self.admins: dict[str, dict] = (self._load(self._admin_path, {}) or {}).get("groups") or {}
        _meta = self._load(self._meta_path, {}) or {}
        self.group_meta: dict[str, dict] = _meta.get("groups") or {}   # group_openid -> {name, alias, platform_id, name_state}
        self.bot_meta: dict[str, dict] = _meta.get("bots") or {}       # platform_id -> {name}
        self._kw_cache: dict[str, Any] = {}   # 文本哈希 -> {"verdict":..., "ts":...}
        self._seq: int = self._max_verdict_id()

    # ---------------- 审核提醒名单（管理员登记） ----------------
    def _save_admins(self) -> None:
        self._save(self._admin_path, {"groups": self.admins})

    def get_admins(self, group: str) -> list[dict]:
        """本群已登记的管理员列表（按登记时间升序）。未登记过则返回空列表。"""
        raw = self.admins.get(group) or {}
        out = []
        for openid, info in raw.items():
            info = info or {}
            out.append({
                "openid": str(openid),
                "name": str(info.get("name") or ""),
                "role": str(info.get("role") or ""),
                "at": int(info.get("at") or 0),
                "by": str(info.get("by") or ""),
            })
        return sorted(out, key=lambda x: x["at"])

    def register_admin(self, group: str, openid: str, *, name: str = "",
                       role: str = "", by: str = "") -> dict:
        """登记/更新一条管理员（幂等：重复登记只刷新姓名与时间）。"""
        if not group or not openid:
            return {}
        rec = {"name": str(name or "")[:64], "role": str(role or "")[:16],
               "at": int(time.time()), "by": str(by or "")[:64]}
        self.admins.setdefault(group, {})[str(openid)] = rec
        self._save_admins()
        return rec

    def unregister_admin(self, group: str, openid: str) -> bool:
        """移出提醒名单。返回是否真的移除了一条。"""
        got = (self.admins.get(group) or {}).pop(str(openid), None)
        if got is None:
            return False
        self._save_admins()
        return True

    # ---------------- 变体累计升级（strike） ----------------
    def variant_strike_summary(self, group: str, member: str, *,
                               window_days: int = 7,
                               exclude_id: Optional[int] = None) -> dict:
        """窗口内某成员的「疑似变体」累计情况（派生统计，不额外落盘）。

        口径：variant_hit 命中 + 非试运行 + 未被人工标误判。
        特意**不要求** executed（变体命中本来就不执行禁言），也不看撤回是否成功
        ——撤不掉（超 2 分钟时限）依然是一次疑似规避。
        只要把记录删掉或标成误判，累计值立刻回落，可回溯、可撤销。
        """
        out = {"count": 0, "confirmed": 0, "last_notified": 0, "last_muted": 0}
        if not group or not member:
            return out
        try:
            win = max(1, int(window_days)) * 86400
        except (TypeError, ValueError):
            win = 7 * 86400
        since = int(time.time()) - win
        for v in self.verdicts:
            if v.get("group") != group or str(v.get("target") or "") != str(member):
                continue
            if not v.get("variant_hit") or v.get("dry_run"):
                continue
            if int(v.get("created_at") or 0) < since:
                continue
            if exclude_id is not None and int(v.get("id") or 0) == int(exclude_id):
                continue
            if is_false_positive(v):
                continue
            out["count"] += 1
            # 人工在待办里处理过（todo_state == "done"）＝ 已确认；标「忽略」的不算
            if str(v.get("todo_state") or "") == "done":
                out["confirmed"] += 1
            st = v.get("variant_strike") or {}
            created = int(v.get("created_at") or 0)
            if st.get("notified"):
                out["last_notified"] = max(out["last_notified"], created)
            if st.get("level") == STRIKE_MUTE or is_strike_muted(v):
                out["last_muted"] = max(out["last_muted"], created)
        return out

    # ---------------- 基础 IO ----------------
    def _load(self, path: str, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except Exception as e:
            if self.logger:
                self.logger.error(f"[审核] 读取 {os.path.basename(path)} 失败: {e}")
            return default

    def _save(self, path: str, data) -> None:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            if self.logger:
                self.logger.error(f"[审核] 写入 {os.path.basename(path)} 失败: {e}")

    # ---------------- 群名 / 备注 ----------------
    def _save_meta(self) -> None:
        self._save(self._meta_path, {"groups": self.group_meta, "bots": self.bot_meta})

    def get_group_meta(self, group: str) -> dict:
        return self.group_meta.get(group) or {}

    def set_group_meta(self, group: str, **fields) -> dict:
        if not group:
            return {}
        cur = self.group_meta.setdefault(group, {})
        for k, v in fields.items():
            if v is not None:
                cur[k] = v
        cur["updated_at"] = time.time()
        self._save_meta()
        return cur

    def set_group_alias(self, group: str, alias: str) -> dict:
        """手动备注名（仅面板显示，优先级高于接口拿到的群名）。"""
        return self.set_group_meta(group, alias=(alias or "").strip()[:40])

    def display_name(self, group: str) -> str:
        """群的展示名：备注 > 接口群名 > 空。"""
        m = self.get_group_meta(group)
        return str(m.get("alias") or m.get("name") or "")

    def get_bot_meta(self, platform_id: str) -> dict:
        return self.bot_meta.get(platform_id) or {}

    def set_bot_meta(self, platform_id: str, **fields) -> dict:
        if not platform_id:
            return {}
        cur = self.bot_meta.setdefault(platform_id, {})
        for k, v in fields.items():
            if v is not None:
                cur[k] = v
        self._save_meta()
        return cur

    def _max_verdict_id(self) -> int:
        try:
            return max([int(v.get("id", 0)) for v in self.verdicts] or [0])
        except Exception:
            return 0

    # ---------------- 群规 ----------------
    def get_rules(self, group: str) -> list[dict]:
        return list(self.rules.get(group) or [])

    def set_rules(self, group: str, rules: list[dict]) -> None:
        self.rules[group] = list(rules or [])
        self._save(self._rules_path, self.rules)

    def add_rule(self, group: str, rule: dict) -> dict:
        rules = self.get_rules(group)
        rid = rule.get("id") or self._next_rule_id(rules)
        rule["id"] = rid
        rules.append(rule)
        self.set_rules(group, rules)
        return rule

    @staticmethod
    def _next_rule_id(rules: list[dict]) -> str:
        used = {str(r.get("id")) for r in rules}
        i = 1
        while f"rule_{i:02d}" in used:
            i += 1
        return f"rule_{i:02d}"

    def delete_rule(self, group: str, index_or_id: str) -> Optional[dict]:
        rules = self.get_rules(group)
        for i, r in enumerate(rules):
            if str(r.get("id")) == str(index_or_id):
                removed = rules.pop(i)
                self.set_rules(group, rules)
                return removed
        # 允许用序号（从 1 开始）
        try:
            idx = int(index_or_id) - 1
            if 0 <= idx < len(rules):
                removed = rules.pop(idx)
                self.set_rules(group, rules)
                return removed
        except (TypeError, ValueError):
            pass
        return None

    # ---------------- 举报设置（每群独立） ----------------
    DEFAULT_SETTINGS: dict[str, Any] = {
        "enabled": True,          # 本群是否启用 /举报
        "dry_run": True,          # 试运行：只判定不执行（默认开，防误杀）
        "auto_scan": False,       # 隐藏开关：本群自动巡检（默认关，仅本群生效）
        "auto_scan_llm": False,   # 巡检是否调用模型（默认关，只跑零成本关键词）
        # ---- 维度开关（按「实现分层」拆，不是按显示名拆）----
        # 说明：语义与群规共用同一次模型调用（群规是注入提示词的），无法只关其中一个；
        # 关闭模型层＝只剩关键词层，零 API 成本；关闭图片层＝图片不进提示词，只判文字。
        "enable_keyword": True,   # 零成本层：词库 + 群自定义关键词
        "enable_model": True,     # 模型层：语义 + 群规（关闭后不发起任何模型调用）
        "enable_vision": True,    # 图片层：图片是否参与模型判定（关闭＝只看文字）
        "keep_context": True,     # 静默留存最近消息，供审核取上下文（仅本地）
        "context_size": 6,        # 取多少条上下文
        "confidence_threshold": 0.75,
        "max_mute_seconds": 86400,
    # 撤回尝试的时间窗口（秒）：110 秒 = 官方 2 分钟限制内；0 = 不限，总是尝试
    "recall_max_age_seconds": 110,
        "report_limit_per_hour": 5,   # 每人每小时举报次数上限
        "reply_when_clean": True,     # 不违规时是否也在群里回一句
        "daily_budget": 0.0,          # 每日费用熔断（0=不限）
        "whitelist": [],              # 豁免成员 openid（管理员自动豁免）
        "keywords": {},               # 群自定义关键词 {category: [pattern]}
        # ---- 变体检测（谐音/形近/插符号绕过）----
        # 定位：只产出「疑似」信号，单次命中只做 撤回 + 记录 + 进待办，本身不禁言；
        # 唯一的禁言出口是下面独立开关的「变体累计升级」L2（默认关）。
        # 默认关：先灰度看记录，确认误报率可接受再开自动撤回。
        "variant_check": False,
        "variant_recall": False,       # 命中变体时自动撤回（关＝只记录）
        "variant_min_chars": 3,        # 谐音匹配的最小音节数（2 会明显增加误报）
        "variant_fuzzy": False,        # 允许「多/少/替换 1 个音节」的模糊匹配
        "variant_confusable": True,    # 形近字通道（与谐音通道独立开关）
        "variant_abbr": True,          # 拼音缩写通道（jwx / jiaweixin）；只产低置信疑似
        # ---- 待办（人工兜底队列）----
        "todo_enabled": True,          # 低置信判定 / 变体命中 自动进待办
        # ---- 复犯累进 ----
        "escalate_enabled": False,     # 默认关：开启后同一人重复违规时长会翻倍
        "escalate_window_days": 30,    # 前科统计窗口（天）
        "escalate_step": 1.0,          # 每次前科增加的倍率（1.0 = 每次翻倍）
        "escalate_max_multiplier": 4.0,  # 倍率上限
        # ---- 变体累计升级（variant strike）：窗口内疑似变体次数 → 提醒 / 自动禁言 ----
        # L1（提醒）默认开：零风险，只在被动消息里 @ 已登记管理员 + 待办置顶。
        # L2（禁言）默认关：变体匹配是启发式（谐音/形近/插符号），误判率高于关键词层，
        #    误禁的代价远大于晚禁一分钟，必须由群管自行开启。
        "variant_strike_enabled": True,
        "variant_strike_window_days": 7,       # 累计窗口（天）
        "variant_strike_threshold": 3,         # 提醒阈值（窗口内第几次开始提醒）
        "variant_strike_notify": True,         # 达阈值时被动消息 @ 已登记管理员（无人登记则不发）
        "variant_strike_mute": False,          # L2 自动禁言（默认关）
        "variant_strike_mute_threshold": 5,    # L2 阈值
        "variant_strike_mute_seconds": 600,    # L2 禁言时长（秒）
        "variant_strike_require_confirm": True,  # L2 前需窗口内至少 1 次人工确认过的变体记录
        "variant_strike_cooldown_hours": 6,    # 触发一次 L2 后的冷却（小时，0=不冷却）
    }

    def get_settings(self, group: str) -> dict:
        s = dict(self.DEFAULT_SETTINGS)
        s.update(self.settings.get(group) or {})
        return s

    def set_settings(self, group: str, patches: dict) -> dict:
        cur = self.settings.get(group) or {}
        cur.update(patches or {})
        self.settings[group] = cur
        self._save(self._settings_path, self.settings)
        return self.get_settings(group)

    # ---------------- 话术模板 ----------------
    def get_templates(self, group: str) -> dict:
        """全局默认 -> 全局自定义 -> 本群覆盖。"""
        out = dict(DEFAULT_TEMPLATES)
        out.update(self.templates.get("global") or {})
        out.update((self.templates.get("groups") or {}).get(group) or {})
        return out

    def set_templates(self, scope: str, values: dict) -> dict:
        """scope='global' 或群 openid。"""
        if scope == "global":
            cur = dict(self.templates.get("global") or {})
            cur.update(values or {})
            self.templates["global"] = cur
        else:
            groups = dict(self.templates.get("groups") or {})
            cur = dict(groups.get(scope) or {})
            cur.update(values or {})
            groups[scope] = cur
            self.templates["groups"] = groups
        self._save(self._tpl_path, self.templates)
        return self.templates

    def get_dim_names(self, group: str = "") -> dict:
        out = dict(DEFAULT_DIM_NAMES)
        out.update(self.templates.get("dim_names") or {})
        return out

    def get_cat_names(self, group: str = "") -> dict:
        out = dict(DEFAULT_CAT_NAMES)
        out.update(self.templates.get("cat_names") or {})
        return out

    # ---------------- 判定记录 ----------------
    def add_verdict(self, rec: dict, max_records: int = 5000) -> dict:
        self._seq += 1
        rec = dict(rec or {})
        rec["id"] = self._seq
        rec.setdefault("created_at", int(time.time()))
        self.verdicts.append(rec)
        if len(self.verdicts) > int(max_records):
            del self.verdicts[: len(self.verdicts) - int(max_records)]
        self._save(self._verdict_path, self.verdicts)
        return rec

    def delete_verdict(self, vid: int) -> bool:
        """删除单条判定记录（仅删本地留痕，不影响已生效的禁言/撤回）。"""
        try:
            target = int(vid)
        except (TypeError, ValueError):
            return False
        for i, v in enumerate(self.verdicts):
            if int(v.get("id") or 0) == target:
                del self.verdicts[i]
                self._save(self._verdict_path, self.verdicts)
                return True
        return False

    def update_verdict(self, vid: int, **fields) -> bool:
        """就地更新一条判定记录（用于回填变体累计升级的结果）。"""
        try:
            target = int(vid)
        except (TypeError, ValueError):
            return False
        for v in self.verdicts:
            if int(v.get("id") or 0) == target:
                v.update(fields or {})
                self._save(self._verdict_path, self.verdicts)
                return True
        return False

    def clear_verdicts(self, group: str = "") -> int:
        """清空判定记录；传 group 则只清该群。返回删除条数。"""
        if group:
            keep = [v for v in self.verdicts if v.get("group") != group]
            removed = len(self.verdicts) - len(keep)
        else:
            keep = []
            removed = len(self.verdicts)
        if removed:
            self.verdicts[:] = keep
            self._save(self._verdict_path, self.verdicts)
        return removed

    def list_verdicts(self, group: str = "", limit: int = 100) -> list[dict]:
        items = self.verdicts if not group else [v for v in self.verdicts if v.get("group") == group]
        return list(reversed(items))[: int(limit)]

    # ---------------- 复犯累进：前科统计 ----------------
    def repeat_count(self, group: str, member: str, *, window_days: int = 30,
                     category: Optional[str] = None, exclude_id: Optional[int] = None) -> int:
        """统计某成员在窗口内「已被实际处理」的前科次数。

        只认「判定为违规 + 真执行了（非试运行）」的记录，并且人工标注过误判（feedback=0 或
        标记为误判）的记录不计入；删除过的记录本来就不在列表里。
        """
        if not group or not member:
            return 0
        try:
            win = max(1, int(window_days)) * 86400
        except (TypeError, ValueError):
            win = 30 * 86400
        since = int(time.time()) - win
        cat = str(category or "").strip()
        n = 0
        for v in self.verdicts:
            if v.get("group") != group or str(v.get("target") or "") != str(member):
                continue
            if not v.get("violation") or not v.get("executed") or v.get("dry_run"):
                continue
            if int(v.get("created_at") or 0) < since:
                continue
            if exclude_id is not None and int(v.get("id") or 0) == int(exclude_id):
                continue
            if is_false_positive(v):
                continue
            if cat and str(v.get("category") or "") != cat:
                continue
            n += 1
        return n

    def member_offense_summary(self, group: str, *, window_days: int = 30,
                               limit: int = 20) -> list[dict]:
        """本群窗口内的违规排行（给面板用）：[{target, name, count, last_ts}]。"""
        try:
            win = max(1, int(window_days)) * 86400
        except (TypeError, ValueError):
            win = 30 * 86400
        since = int(time.time()) - win
        agg: dict[str, dict] = {}
        for v in self.verdicts:
            if v.get("group") != group or not v.get("violation") or not v.get("executed"):
                continue
            if int(v.get("created_at") or 0) < since:
                continue
            t = str(v.get("target") or "")
            if not t:
                continue
            rec = agg.setdefault(t, {"target": t, "name": v.get("target_name") or "", "count": 0, "last_ts": 0})
            rec["count"] += 1
            rec["last_ts"] = max(rec["last_ts"], int(v.get("created_at") or 0))
            if not rec["name"] and v.get("target_name"):
                rec["name"] = v["target_name"]
        out = sorted(agg.values(), key=lambda r: (-r["count"], -r["last_ts"]))
        return out[: int(limit)]

    # ---------------- 待办（人工兜底队列） ----------------
    TODO_KIND_LOW_CONF = "low_confidence"
    TODO_KIND_VARIANT = "variant"
    # 待办时效窗口：只把近 N 天内的记录摆上队列。
    # 理由：升级后老的判定记录没有 todo_state，会把几天前、早就没法撤回（时限 110 秒）
    # 也没法禁言的历史账一次性涌进队列；给个窗口，超出的一律不打扰。
    TODO_WINDOW_DAYS = 7

    @staticmethod
    def _todo_kind(v: dict) -> str:
        """判断一条判定记录是否该进待办、属于哪一类；返回空串表示不进。"""
        if str(v.get("todo_state") or "") in ("done", "ignored", "disabled"):
            return ""
        strike = v.get("variant_strike") or {}
        level = str(strike.get("level") or "")
        if level == STRIKE_MUTE or is_strike_muted(v):
            return "variant_strike_mute"
        if level == STRIKE_TODO:
            return "variant_strike"
        if v.get("variant_hit"):
            return "variant"
        if v.get("violation") and not v.get("executed") and not v.get("dry_run"):
            rsn = str(v.get("skipped_reason") or "")
            if "置信度" in rsn or "豁免" in rsn:
                return "low_confidence"
        return ""

    def todo_items(self, group: str, limit: int = 100,
                   within_days: int | None = None) -> list[dict]:
        """聚合本群待办：低置信未处理 + 变体疑似（都还没人工处理过的）。

        within_days：只看近 N 天的记录（默认 TODO_WINDOW_DAYS=7），0 或负数＝不限。
        """
        days = self.TODO_WINDOW_DAYS if within_days is None else int(within_days)
        floor = (int(time.time()) - days * 86400) if days and days > 0 else 0
        out: list[dict] = []
        for v in reversed(self.verdicts):
            if group and v.get("group") != group:
                continue
            if floor and int(v.get("created_at") or 0) < floor:
                continue
            kind = self._todo_kind(v)
            if not kind:
                continue
            out.append({
                "id": v.get("id"),
                "kind": kind,
                "created_at": v.get("created_at"),
                "target": v.get("target") or "",
                "target_name": v.get("target_name") or "",
                "text": v.get("text") or "",
                "category": v.get("category") or "",
                "reason": v.get("reason") or "",
                "confidence": v.get("confidence") or 0,
                "variant_method": (v.get("variant") or {}).get("method") or "",
                "variant_strength": (v.get("variant") or {}).get("strength") or "",
                "variant_recalled": bool(v.get("variant_recalled")),
                "variant_strike": v.get("variant_strike") or None,
                "variant_strike_count": int((v.get("variant_strike") or {}).get("count") or 0),
                "suggest_seconds": severity_to_seconds(str(v.get("severity") or "medium")),
                "reporter": v.get("reporter") or "",
            })
            if len(out) >= int(limit):
                break
        return out

    def todo_count(self, group: str = "", within_days: int | None = None) -> int:
        return len(self.todo_items(group, limit=1000, within_days=within_days))

    def mark_todo(self, ids: list, state: str = "done", *, operator: str = "",
                  note: str = "") -> int:
        """把若干条记录标记为已处理/已忽略。返回改动条数。"""
        want = set()
        for i in (ids or []):
            try:
                want.add(int(i))
            except (TypeError, ValueError):
                continue
        if not want:
            return 0
        changed = 0
        now = int(time.time())
        for v in self.verdicts:
            if int(v.get("id") or 0) in want:
                v["todo_state"] = str(state or "done")
                v["todo_by"] = str(operator or "")[:64]
                v["todo_note"] = str(note or "")[:200]
                v["todo_at"] = now
                changed += 1
        if changed:
            self._save(self._verdict_path, self.verdicts)
        return changed

    def stats(self, group: str = "") -> dict:
        items = self.verdicts if not group else [v for v in self.verdicts if v.get("group") == group]
        total = len(items)
        hit = sum(1 for v in items if v.get("violation"))
        cached = sum(1 for v in items if v.get("from_cache"))
        cost = sum(float(v.get("cost") or 0) for v in items)
        tokens = sum(int(v.get("tokens") or 0) for v in items)
        today = int(time.time()) - 86400
        today_items = [v for v in items if int(v.get("created_at") or 0) >= today]
        return {
            "total": total,
            "hit": hit,
            "clean": total - hit,
            "from_cache": cached,
            "cache_rate": round(cached / total, 3) if total else 0.0,
            "cost": round(cost, 4),
            "tokens": tokens,
            "today_total": len(today_items),
            "today_hit": sum(1 for v in today_items if v.get("violation")),
            "today_cost": round(sum(float(v.get("cost") or 0) for v in today_items), 4),
            "today_tokens": sum(int(v.get("tokens") or 0) for v in today_items),
            "variant_hits": sum(1 for v in items if v.get("variant_hit")),
            "variant_today": sum(1 for v in today_items if v.get("variant_hit")),
            # 变体累计升级触发过自动禁言的条数（口径见 is_strike_muted，兼容新旧字段）
            "variant_strike_muted": sum(1 for v in items if is_strike_muted(v)),
            "todo_open": self.todo_count(group),
        }

    def groups_overview(self) -> list[dict]:
        groups = set(self.rules.keys()) | set(self.settings.keys())
        groups |= set((self.templates.get("groups") or {}).keys())
        groups |= {v.get("group") for v in self.verdicts if v.get("group")}
        out = []
        for g in sorted(x for x in groups if x):
            st = self.get_settings(g)
            rules = self.get_rules(g)
            out.append(
                {
                    "group": g,
                    "enabled": bool(st.get("enabled")),
                    "dry_run": bool(st.get("dry_run")),
                    "auto_scan": bool(st.get("auto_scan")),
                    "rule_count": len(rules),
                    "rules": rules,
                    "settings": st,
                    "stats": self.stats(g),
                }
            )
        return out

    # ---------------- 判定缓存（L1） ----------------
    def cache_get(self, text: str, ttl: int = DEFAULT_CACHE_TTL) -> Optional[dict]:
        item = self._kw_cache.get(text_hash(text))
        if not item:
            return None
        if int(time.time()) - int(item.get("ts") or 0) > int(ttl):
            self._kw_cache.pop(text_hash(text), None)
            return None
        return dict(item.get("verdict") or {})

    def cache_put(self, text: str, verdict: dict, cap: int = 2000) -> None:
        if not text or not verdict or not verdict.get("violation"):
            return  # 只缓存"判违规"的结果，避免误放过
        self._kw_cache[text_hash(text)] = {"verdict": dict(verdict), "ts": int(time.time())}
        if len(self._kw_cache) > cap:
            oldest = sorted(self._kw_cache.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in oldest[: len(self._kw_cache) - cap]:
                self._kw_cache.pop(k, None)

    # ---------------- 举报限频 ----------------
    def report_allowed(self, group: str, member: str, limit_per_hour: int) -> bool:
        if int(limit_per_hour or 0) <= 0:
            return True
        now = int(time.time())
        key = f"{group}:{member}"
        bucket = getattr(self, "_report_bucket", None)
        if bucket is None:
            bucket = {}
            self._report_bucket = bucket
        lst = [t for t in (bucket.get(key) or []) if now - int(t) < 3600]
        if len(lst) >= int(limit_per_hour):
            bucket[key] = lst
            return False
        lst.append(now)
        bucket[key] = lst
        return True
