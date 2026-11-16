"""处置动作扩展点（Action）。

内置动作（警告/撤回/禁言/转待办）仍由 `policy` + `engine` 决定，保持"AI 定性质、代码定刑期"。
本注册表用于**追加**动作（例如：记分、通知外部审计、联动工单），默认空 → 零行为变化。
动作执行失败不会影响主处罚与判定记录。
"""

from __future__ import annotations

from typing import Protocol


class Action(Protocol):
    name: str

    async def apply(self, ctx: dict) -> dict | None:  # pragma: no cover - 协议
        ...


_REGISTRY: list[Action] = []


def register(action: Action) -> None:
    global _REGISTRY
    _REGISTRY = [a for a in _REGISTRY if getattr(a, "name", "") != getattr(action, "name", "")]
    _REGISTRY.append(action)


def clear() -> None:
    _REGISTRY.clear()


def count() -> int:
    return len(_REGISTRY)


async def run(host, ctx: dict) -> list[dict]:
    """执行所有扩展动作，返回各自结果；异常隔离。"""
    out = []
    for a in list(_REGISTRY):
        try:
            r = await a.apply({**ctx, "host": host})
            if r is not None:
                out.append({"name": getattr(a, "name", "?"), "result": r})
        except Exception as e:  # noqa: BLE001
            try:
                host.logger.warning(f"[审核][扩展] 动作 {getattr(a, 'name', '?')} 异常：{e}")
            except Exception:
                pass
    return out
