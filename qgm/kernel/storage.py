"""持久化基础操作：原子写 JSON、容错读 JSON。

业务层只应通过这里落盘，避免各处 `open(..., "w")` 在崩溃/掉电时留下半截文件。
"""

from __future__ import annotations

import json
import os


def write_json_atomic(path: str, payload, *, indent: int = 1) -> None:
    """原子写 JSON：先写临时文件，fsync 后用 os.replace 顶上去。

    这些文件（群列表 / 禁言历史 / 撤回历史 / 自我介绍状态）都是整份重写，
    直接 open(w) 在写一半时崩溃会留下被截断的 JSON，下次启动解析失败 → 数据静默清零。
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: str, default=None):
    """读 JSON；文件不存在或解析失败时返回 default（不抛异常）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
