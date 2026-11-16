"""检测维度扩展点（Detector）。

设计目标：新增一个检测维度 = 实现 `Detector` 并 `register()`，不改 pipeline 主体。

规则：
- 注册表**默认为空**，此时 `collect()` 不产生任何开销，行为与 1.6.0 完全一致；
- 扩展检测**只增强、不削弱**：只能把「未违规 → 违规」或提高严重度/置信度，
  不会把已有违规判成无违规（避免第三方检测器静默放行）；
- 单个 detector 抛异常只记 warning，不影响本次判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

_SEVERITY_RANK = {"": 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class Evidence:
    name: str
    violation: bool = False
    severity: str = ""
    category: str = ""
    confidence: float = 0.0
    detail: str = ""
    extra: dict = field(default_factory=dict)


class Detector(Protocol):
    name: str

    async def detect(self, ctx: dict) -> Evidence | None:  # pragma: no cover - 协议
        ...


_REGISTRY: list[Detector] = []


def register(detector: Detector) -> None:
    """注册一个检测维度（重复 name 会覆盖旧的）。"""
    global _REGISTRY
    _REGISTRY = [d for d in _REGISTRY if getattr(d, "name", "") != getattr(detector, "name", "")]
    _REGISTRY.append(detector)


def unregister(name: str) -> None:
    global _REGISTRY
    _REGISTRY = [d for d in _REGISTRY if getattr(d, "name", "") != name]


def clear() -> None:
    """测试用：清空注册表。"""
    _REGISTRY.clear()


def count() -> int:
    return len(_REGISTRY)


def names() -> list[str]:
    return [getattr(d, "name", "?") for d in _REGISTRY]


async def collect(host, ctx: dict) -> list[Evidence]:
    out: list[Evidence] = []
    for d in list(_REGISTRY):
        try:
            ev = await d.detect({**ctx, "host": host})
        except Exception as e:  # noqa: BLE001 - 扩展检测不应影响主流程
            try:
                host.logger.warning(f"[审核][扩展] 检测器 {getattr(d, 'name', '?')} 异常：{e}")
            except Exception:
                pass
            continue
        if ev:
            out.append(ev)
    return out


def merge(verdict: dict, evidences: list[Evidence]) -> dict:
    """把扩展检测结果并入判定：只升不降。"""
    if not evidences:
        return verdict
    out = dict(verdict or {})
    extra = []
    for ev in evidences:
        extra.append({"name": ev.name, "violation": ev.violation, "severity": ev.severity,
                      "category": ev.category, "confidence": ev.confidence, "detail": ev.detail})
        if not ev.violation:
            continue
        if not out.get("violation"):
            out["violation"] = True
            out["basis"] = out.get("basis") or f"detector:{ev.name}"
        if _SEVERITY_RANK.get(str(ev.severity).lower(), 0) > _SEVERITY_RANK.get(str(out.get("severity") or "").lower(), 0):
            out["severity"] = ev.severity
        if ev.category and not out.get("category"):
            out["category"] = ev.category
        if float(ev.confidence or 0) > float(out.get("confidence") or 0):
            out["confidence"] = ev.confidence
    out["extra_evidence"] = extra
    return out
