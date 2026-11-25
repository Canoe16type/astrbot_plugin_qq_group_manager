"""指令路由表：把「指令 + 参数」解析成「处理方法名 + 参数」。

纯函数、无 IO，便于单测；加新指令只需在这里增加一条映射，不用再动 200 行的 if/elif。
"""

from __future__ import annotations

UNMUTE_ALIASES = ("解除", "解禁", "unmute")
MUTE_LIST_ALIASES = ("名单", "列表", "list", "查")
MUTE_HISTORY_ALIASES = ("历史", "history", "记录", "流水")
APPROVE_ALIASES = ("通过", "同意", "approve", "过")
REJECT_ALIASES = ("拒绝", "驳回", "reject", "拒")


def _sub(parts: list[str]) -> str:
    return parts[1].lower() if len(parts) > 1 else ""


def resolve_admin(cmd: str, parts: list[str]):
    """管理员指令路由：返回 (方法名, 参数) 或 None。"""
    if cmd == "/审核":
        return "_cmd_mod", parts
    if cmd == "/禁言":
        sub = _sub(parts)
        if sub in UNMUTE_ALIASES:
            return "_cmd_unmute", ["/禁言"] + parts[2:]
        if sub in MUTE_LIST_ALIASES:
            return "_cmd_list_mutes", parts
        if sub in MUTE_HISTORY_ALIASES:
            return "_cmd_list_mute_history", ["/禁言"] + parts[2:]
        return "_cmd_mute", parts
    if cmd == "/审批":
        sub = _sub(parts)
        if sub in APPROVE_ALIASES:
            return "_cmd_review", ["/通过"] + parts[2:]
        if sub in REJECT_ALIASES:
            return "_cmd_review", ["/拒绝"] + parts[2:]
        return "_cmd_list_requests", parts
    if cmd == "/状态":
        return "_cmd_status", parts
    if cmd == "/撤销":
        return "_cmd_revoke", parts
    if cmd == "/撤回":
        return "_cmd_recall", parts
    if cmd == "/管理登记":
        return "_cmd_admin_register", parts
    if cmd == "/踢人":
        return "_cmd_kick", parts
    if cmd == "/成员":
        return "_cmd_member", parts
    if cmd == "/记录":
        return "_cmd_records", parts
    return None
