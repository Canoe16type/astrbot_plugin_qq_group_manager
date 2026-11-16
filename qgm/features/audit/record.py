"""判定记录的表现层工具：单行摘要、原文归档、导出筛选。

只做纯计算，不碰磁盘；读写仍由 `mod` 持有的 JSONL 存储负责。
"""

from __future__ import annotations

import time


def verdict_text(rep: dict, cap: int = 1000) -> dict:
    """判定记录要留的「原始内容」：够人工二次确认，又不至于把记录撑爆。"""
    raw = str(rep.get("text") or "")
    return {
        "text": raw[:cap],
        "text_len": len(raw),
        "text_truncated": len(raw) > cap,
        "images": len(rep.get("images") or []),
    }


def fmt_verdict_line(v: dict, cat_names: dict | None = None, fmt_duration=None) -> str:
    """把一条判定记录压成一行，供「撤销 列表」与回执使用。"""
    names = cat_names or {}
    act = str(v.get("action") or ("mute" if v.get("executed") else "none"))
    if act == "mute":
        secs = int(v.get("ban_seconds") or 0)
        act_txt = f"禁言 {fmt_duration(secs) if fmt_duration else f'{secs}s'}"
    else:
        act_txt = {
            "warn": "警告", "recall": "已撤回", "todo": "转人工待办", "none": "未处罚",
        }.get(act, act)
    if v.get("revoked"):
        act_txt = "已撤销"

    ts = time.strftime("%m-%d %H:%M", time.localtime(int(v.get("created_at") or 0)))
    verdict = names.get(str(v.get("category") or ""), "违规") if v.get("violation") else "未违规"
    target = v.get("target_name") or v.get("target") or "-"
    conf = float(v.get("confidence") or 0)
    return f"#{v.get('id')} {ts} {target}｜{verdict}｜{act_txt}｜置信度 {conf:.2f}"


def filter_records(items, *, group: str = "", since: int = 0, limit: int = 1000) -> list[dict]:
    """按群/时间筛选（输入新→旧），输出仍按旧→新，与旧导出行为一致。"""
    picked: list[dict] = []
    for v in items:
        if group and v.get("group") != group:
            continue
        if since and int(v.get("created_at") or 0) < since:
            continue
        picked.append(v)
        if len(picked) >= limit:
            break
    picked.reverse()
    return picked
