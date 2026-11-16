"""时间/时长格式化（纯函数）。"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ACTIVE_TZ_NAME = "Asia/Shanghai"


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
