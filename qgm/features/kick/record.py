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


def recent(host, group: str = "", limit: int = 10) -> list[dict]:
    data = storage.read_json(str(_path(host)), default={}) or {}
    rows = data.get("records") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return []
    picked = [r for r in rows if not group or r.get("group") == group]
    return list(reversed(picked))[:max(1, min(50, limit))]
