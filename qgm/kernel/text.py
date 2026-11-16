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


def _recall_hint(err: object) -> str:
    """把 QQ 官方撤回接口的错误码翻译成人话（官方文档「撤回群聊消息」）。"""
    s = str(err)
    if "40064004" in s or "撤回时限" in s:
        return "官方 2 分钟撤回时限已过（错误码 40064004）"
    if "40062003" in s or "无操作权限" in s:
        return ("机器人无撤回权限（错误码 40062003）：需把机器人设为群管理员，"
                "且被撤回者需为普通群成员（群主/管理员的消息撤不了）")
    if "50065001" in s:
        return "平台侧撤回失败，可稍后重试（错误码 50065001）"
    return s or "撤回失败"

def _refresh_hint(updated: int, denied: int, no_bot: int, failed: list, first_error: str,
                  skipped: int = 0) -> str:
    """把拉取结果翻译成一句人话，给页面直接显示。"""
    tail = f"；{skipped} 个群已有名字，未重复请求接口" if skipped else ""
    if not (updated or denied or failed or no_bot):
        return (f"没有需要拉取的群{tail}" if skipped else "没有需要拉取的群")
    if updated and not denied and not failed:
        return f"已更新 {updated} 个群名{tail}"
    if denied:
        return (f"更新 {updated} 个群名；{denied} 个群取不到名字——群信息接口（/v2/groups/{{openid}}/info）"
                f"仅白名单机器人可用（错误码 11253，需向平台运营申请），这些群可在页面里手动备注{tail}")
    if no_bot:
        return f"更新 {updated} 个群名；{no_bot} 个群未能确定所属机器人，无法拉取{tail}"
    if failed:
        return f"更新 {updated} 个群名；{len(failed)} 个失败：{first_error[:120]}{tail}"
    return f"没有需要拉取的群{tail}"


_PURGE_LABELS = {
    "known_groups": "已知群记录",
    "mute_history": "禁言历史",
    "recall_history": "撤回历史",
    "members": "成员档案",
    "rules": "群规",
    "settings": "审核设置",
    "verdicts": "判定记录",
    "group_meta": "群名/备注",
    "admins": "提醒名单",
    "templates": "群话术覆盖",
    "welcome_dedupe": "入群欢迎去重",
    "notify_task": "待推送通知",
    "nick_cache": "昵称缓存",
    "pending": "申请缓存",
}

def _purge_summary_text(summary: dict | None, empty: str = "无残留数据") -> str:
    """把清理结果字典拼成一句人能读的话。"""
    if not summary:
        return empty
    parts = []
    for k, v in summary.items():
        label = _PURGE_LABELS.get(k, k)
        try:
            n = int(v or 0)
        except (TypeError, ValueError):
            n = 0
        parts.append(f"{label} {n} 条" if n > 1 else label)
    return "、".join(parts)
