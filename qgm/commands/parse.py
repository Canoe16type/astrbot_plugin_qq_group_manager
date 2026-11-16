"""指令文本解析：统一 `/群管 <子指令> ...` 矩阵。

设计约束：
- 用户侧只有 `/群管` 一个入口，子指令由 `qgm_group.command(...)` 注册；
- 进入 `_handle_command` 时通过 `forced_cmd + skip=1` 归一成内部形式，
  例如 `/群管 禁言 名单` → `("/禁言", ["/禁言", "名单"])`。
"""

from __future__ import annotations

import re

_MENTION_RE = re.compile(r"<@!?[0-9A-Za-z_-]+>")


def clean_mentions(text: str) -> str:
    """QQ 官方消息里的 @ 是 `<@openid>` 文本；解析参数前统一去掉。"""
    return _MENTION_RE.sub(" ", text or "").strip()


def parse_command(text: str, forced_cmd: str | None = None, skip: int = 0):
    """返回 (cmd, parts)。cmd 一定以 / 开头（或原样返回 /群管）。"""
    parts = clean_mentions(text).split()
    if forced_cmd:
        rest = parts[1 + skip:] if parts else []
        return forced_cmd, [forced_cmd] + rest
    if not parts:
        return "", []
    cmd = parts[0]
    if not cmd.startswith("/"):
        cmd = "/" + cmd
    return cmd, parts
