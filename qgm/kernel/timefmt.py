"""时间/时长格式化（纯函数）。"""

from __future__ import annotations

from typing import Optional

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ACTIVE_TZ_NAME = "Asia/Shanghai"


def active_timezone_name() -> str:
    return _ACTIVE_TZ_NAME


def fmt_ts(ts: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """unix 时间戳 -> 本地可读时间。"""
    try:
        return datetime.fromtimestamp(ts).strftime(fmt)
    except Exception:
        return str(ts)


def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分钟"
    if seconds < 86400:
        return f"{seconds // 3600}小时"
    return f"{seconds // 86400}天"


def _active_tz():
    try:
        return ZoneInfo(_ACTIVE_TZ_NAME)
    except Exception:
        return None

def set_active_timezone(name: str) -> str:
    """写入生效时区；非法时区名回退 Asia/Shanghai。"""
    global _ACTIVE_TZ_NAME
    name = str(name or "").strip() or "Asia/Shanghai"
    try:
        ZoneInfo(name)
    except Exception:
        name = "Asia/Shanghai"
    _ACTIVE_TZ_NAME = name
    return name

def fmt_rfc3339(rfc: str) -> str:
    """RFC3339 -> 按 AstrBot 配置时区渲染的可读时间。"""
    try:
        # 兼容 Z / +08:00 结尾
        s = rfc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        tz = _active_tz()
        dt = dt.astimezone(tz) if tz is not None else dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return rfc


DURATION_RE = re.compile(r"^\s*(\d+)\s*(秒|分钟|分|小时|时|天|s|m|h|d)?\s*$", re.I)

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

def parse_duration(text: str) -> Optional[int]:
    """解析时长文本为秒数。支持 30秒/5分钟/2小时/1天/600(秒)/一分钟。"""
    m = DURATION_RE.match(text or "")
    if not m:
        # 兼容中文数字写法："一分钟"→60s、"半小时"→1800s（十以上暂不展开）
        t = (text or "").strip()
        if len(t) == 3 and t[0] in _CN_NUM and t[1:] in ("秒", "分", "分钟", "时", "小时", "天"):
            return parse_duration(f"{_CN_NUM[t[0]]}{t[1:]}")
        if len(t) == 3 and t[0] == "半":
            if t[1:] in ("分", "分钟"):
                return 30
            if t[1:] in ("时", "小时"):
                return 30 * 60
        return None
    num = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit in ("s", "秒"):
        return num
    if unit in ("m", "分钟", "分"):
        return num * 60
    if unit in ("h", "小时", "时"):
        return num * 3600
    if unit in ("d", "天"):
        return num * 86400
    return num
