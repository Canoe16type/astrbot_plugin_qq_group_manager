"""文本与标识符工具（无 IO、无全局状态）。"""

from __future__ import annotations


def short_id(value, n: int = 6) -> str:
    """日志用短标识：统一取后 n 位；空值返回 "-"。"""
    text = str(value or "")
    return text[-n:] if text else "-"


def clip(text, limit: int) -> str:
    """截断到 limit 字符，超出部分用省略号表示。"""
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…"
