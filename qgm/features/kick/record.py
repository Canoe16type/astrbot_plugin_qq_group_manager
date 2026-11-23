"""踢人记录（JSON 持久化，按插件数据目录存放）。"""

from __future__ import annotations

import time
from pathlib import Path

from ...kernel import storage

MAX_RECORDS = 1000


def _path(host) -> Path:
    return Path(host._plugin_data_dir()) / "kick_history.json"


def add(host, record: dict) -> dict:
    rec = {"id": int(time.time() * 1000), "ts": time.time(), **record}
    data = storage.read_json(str(_path(host)), default={}) or {}
    rows = data.get("records") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = []
    rows.append(rec)
    data = {"version": 1, "records": rows[-MAX_RECORDS:]}
    storage.write_json_atomic(str(_path(host)), data)
    return rec


def iter_rows(host, group: str = "") -> list[dict]:
    """全部记录（可选按群过滤），供统一记录查询层使用。"""
    data = storage.read_json(str(_path(host)), default={}) or {}
    rows = data.get("records") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if not group or r.get("group") == group]


def clear(host, group: str = "") -> int:
    """清空踢人记录；传 group 只清该群。返回删除条数。"""
    data = storage.read_json(str(_path(host)), default={}) or {}
    rows = data.get("records") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return 0
    keep = [r for r in rows if group and r.get("group") != group]
    removed = len(rows) - len(keep)
    if removed:
        storage.write_json_atomic(str(_path(host)), {"version": 1, "records": keep})
    return removed


def recent(host, group: str = "", limit: int = 10) -> list[dict]:
    data = storage.read_json(str(_path(host)), default={}) or {}
    rows = data.get("records") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return []
    picked = [r for r in rows if not group or r.get("group") == group]
    return list(reversed(picked))[:max(1, min(50, limit))]
