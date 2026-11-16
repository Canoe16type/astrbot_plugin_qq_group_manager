"""时间/时长格式化（纯函数）。"""

from __future__ import annotations

from datetime import datetime


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
