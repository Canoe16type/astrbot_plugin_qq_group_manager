"""群数据清理。"""

from __future__ import annotations

from ...kernel.text import _purge_summary_text


def _purge_group_data(host, group, pid='', reason=''):
        """把一个群在本插件里的所有痕迹清掉，返回各表删除数量。

        覆盖：已知群列表、群管自我介绍状态、禁言历史、撤回历史、成员档案、
        群规/审核设置/判定记录/群名备注/提醒名单、入群欢迎去重、各种
        按群缓存与待推送任务。
        """
        group = str(group or "")
        if not group:
            return {}
        reason = reason or "手动清理"
        out: dict[str, object] = {}

        # ① 已知群列表
        if host._known_groups.pop(group, None) is not None:
            host._save_known_groups()
            out["known_groups"] = 1

        # ② 群管自我介绍状态（发过的标记 / 待兜底标记）
        touched = False
        if host._intro_done.pop(group, None) is not None:
            touched = True
        if host._intro_pending.pop(group, None) is not None:
            touched = True
        if touched:
            host._save_intro_state()

        # ③ 禁言历史 / 撤回历史
        if host._mute_history.pop(group, None) is not None:
            host._save_mute_history()
            out["mute_history"] = 1
        if host._recall_history.pop(group, None) is not None:
            host._save_recall_history()
            out["recall_history"] = 1

        # ④ 成员档案
        try:
            n = host.members.purge_group(group)
            if n:
                out["members"] = n
        except Exception as e:                       # noqa: BLE001
            host.logger.warning(f"[清理] 成员档案清理失败: {e}")

        # ⑤ 群规 / 审核设置 / 判定记录 / 群名备注 / 提醒名单 / 群模板
        try:
            removed = host.mod.purge_group(group)
            if any(int(v or 0) for v in removed.values()):
                out.update({k: v for k, v in removed.items() if int(v or 0)})
        except Exception as e:                       # noqa: BLE001
            host.logger.warning(f"[清理] 审核数据清理失败: {e}")

        # ⑥ 入群欢迎去重表（键是 "群:成员"）
        prefix = f"{group}:"
        stale = [k for k in host._join_welcomed if k.startswith(prefix)]
        for k in stale:
            host._join_welcomed.pop(k, None)
        if stale:
            out["welcome_dedupe"] = len(stale)

        # ⑦ 按群内存缓存 / 待推送任务
        for attr in (
            "_nick_cache", "_pending", "_notified", "_seen_requests",
            "_join_error_code", "_join_backoff", "_join_event_at",
            "_join_fresh", "_recent_group_msgs",
        ):
            bucket = getattr(host, attr, None)
            if isinstance(bucket, dict) and bucket.pop(group, None) is not None:
                out[attr.lstrip("_")] = 1
        for attr in ("_member_perm_blocked", "_join_denied_logged"):
            bucket = getattr(host, attr, None)
            if isinstance(bucket, set):
                bucket.discard(group)
        task = host._join_notify_tasks.pop(group, None)
        if task is not None and not task.done():
            task.cancel()
            out["notify_task"] = 1
        if host._last_active_group == group:
            host._last_active_group = ""

        host.logger.info(f"[清理] 群 {group} 数据已清理（{reason}）：{_purge_summary_text(out)}")
        return out
