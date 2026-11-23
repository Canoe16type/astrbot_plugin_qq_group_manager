"""统一记录查询层。

只读现有四类存储，不迁移、不改变写入格式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..kick import record as kick_record


KINDS = ("verdict", "mute", "recall", "kick")
PREFIX = {"verdict": "V", "mute": "M", "recall": "R", "kick": "K"}


@dataclass(frozen=True)
class RecordRow:
    kind: str
    id: int
    display_id: str
    group: str
    ts: float
    target_ids: tuple[str, ...] = ()
    target_name: str = ""
    actor_id: str = ""
    actor_name: str = ""
    action: str = ""
    result: str = ""
    ok: Optional[bool] = None
    reason: str = ""
    detail: dict = field(default_factory=dict)
    legacy: bool = False

    @property
    def target_id(self) -> str:
        return self.target_ids[0] if self.target_ids else ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "id": self.id, "display_id": self.display_id,
            "group": self.group, "ts": self.ts, "target_ids": list(self.target_ids),
            "target_name": self.target_name, "actor_id": self.actor_id,
            "actor_name": self.actor_name, "action": self.action,
            "result": self.result, "ok": self.ok, "reason": self.reason,
            "detail": dict(self.detail), "legacy": self.legacy,
        }


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _verdict_row(rec: dict) -> RecordRow:
    target = str(rec.get("target") or "")
    executed = bool(rec.get("executed"))
    dry = bool(rec.get("dry_run"))
    if dry:
        result = "试运行"
    elif executed:
        result = "已执行"
    elif rec.get("pending_review"):
        result = "待人工"
    else:
        result = "未执行"
    return RecordRow(
        kind="verdict", id=_int(rec.get("id")), display_id="",
        group=str(rec.get("group") or ""), ts=_float(rec.get("created_at")),
        target_ids=(target,) if target else (), target_name=str(rec.get("target_name") or ""),
        actor_id=str(rec.get("operator") or ""), actor_name=str(rec.get("operator_name") or "自动审核"),
        action=str(rec.get("action") or "none"), result=result, ok=executed,
        reason=str(rec.get("reason") or ""),
        detail={
            "category": rec.get("category") or "",
            "category_public": rec.get("category_public") or "",
            "severity_label": rec.get("severity_label") or "",
            "rule_name": rec.get("rule_name") or "",
            "ban_seconds": _int(rec.get("ban_seconds")),
            "pending_review": bool(rec.get("pending_review")),
            "dry_run": dry,
            "match_by": rec.get("match_by") or "",
        },
        legacy=not bool(target),
    )


def _mute_row(rec: dict) -> RecordRow:
    target = str(rec.get("member_openid") or "")
    status = str(rec.get("status") or "active")
    result = {"active": "禁言中", "unmuted": "已解禁", "expired": "已到期"}.get(status, status)
    return RecordRow(
        kind="mute", id=_int(rec.get("id")), display_id="",
        group=str(rec.get("group_openid") or ""), ts=_float(rec.get("mute_at")),
        target_ids=(target,) if target else (), target_name=str(rec.get("username") or ""),
        actor_id=str(rec.get("operator") or ""), actor_name=str(rec.get("operator_name") or ""),
        action="mute", result=result, ok=True, reason=str(rec.get("reason") or ""),
        detail={
            "seconds": _int(rec.get("seconds")),
            "duration_text": rec.get("duration_text") or "",
            "status": status,
            "expire_at": _float(rec.get("expire_at")),
        },
        legacy=not bool(target),
    )


def _recall_row(rec: dict) -> RecordRow:
    target = str(rec.get("target_openid") or "")
    return RecordRow(
        kind="recall", id=_int(rec.get("id")), display_id="",
        group=str(rec.get("group_openid") or ""), ts=_float(rec.get("recalled_at")),
        target_ids=(target,) if target else (), target_name=str(rec.get("target_name") or ""),
        actor_id=str(rec.get("operator") or ""), actor_name=str(rec.get("operator_name") or ""),
        action="recall", result="已撤回", ok=True, reason="",
        detail={
            "source": rec.get("source") or "",
            "message_id": rec.get("message_id") or "",
            "event_id": rec.get("event_id") or "",
        },
        legacy=not bool(target),
    )


def _kick_row(rec: dict) -> RecordRow:
    targets = rec.get("targets") if isinstance(rec.get("targets"), list) else []
    ids = tuple(str(t.get("openid") or "") for t in targets if isinstance(t, dict) and t.get("openid"))
    names = "、".join(str(t.get("name") or "") for t in targets if isinstance(t, dict) and t.get("name"))
    ok = bool(rec.get("ok"))
    return RecordRow(
        kind="kick", id=_int(rec.get("id")), display_id="",
        group=str(rec.get("group") or ""), ts=_float(rec.get("ts")),
        target_ids=ids, target_name=names,
        actor_id=str(rec.get("operator") or ""), actor_name=str(rec.get("operator_name") or ""),
        action="kick", result="已踢出" if ok else "失败", ok=ok,
        reason=str(rec.get("reason") or ""),
        detail={
            "error_code": rec.get("err_code") or "",
            "error_msg": rec.get("err_msg") or "",
            "count": len(ids),
        },
        legacy=not bool(ids),
    )


def _with_display(row: RecordRow) -> RecordRow:
    return RecordRow(**{**row.__dict__, "display_id": f"{PREFIX[row.kind]}{row.id}"})


def iter_rows(host, group: str = "") -> Iterable[RecordRow]:
    """按群遍历全部记录（跨类型），不排序。"""
    for rec in host.mod.list_verdicts(group=group, limit=1_000_000):
        yield _with_display(_verdict_row(rec))
    for rec in (host._mute_history.get(group) or []) if group else [
        r for lst in host._mute_history.values() for r in lst
    ]:
        yield _with_display(_mute_row(rec))
    for rec in (host._recall_history.get(group) or []) if group else [
        r for lst in host._recall_history.values() for r in lst
    ]:
        yield _with_display(_recall_row(rec))
    for rec in kick_record.iter_rows(host, group=group):
        yield _with_display(_kick_row(rec))


def _filtered(host, group: str = "", kinds: Optional[Iterable[str]] = None,
              target_id: str = "", target_text: str = "", since: float = 0.0) -> list[RecordRow]:
    want = {str(k) for k in kinds} if kinds else set(KINDS)
    want &= set(KINDS)
    q = str(target_text or "").strip().lower()
    out = []
    for row in iter_rows(host, group=group):
        if row.kind not in want:
            continue
        if target_id and target_id not in row.target_ids:
            continue
        if q:
            names = str(row.target_name or "").lower()
            if q not in names and not any(str(t).lower().endswith(q) for t in row.target_ids):
                continue
        if since and row.ts < since:
            continue
        out.append(row)
    out.sort(key=lambda r: (r.ts, r.id), reverse=True)
    return out


def query(host, group: str = "", kinds: Optional[Iterable[str]] = None,
          target_id: str = "", target_text: str = "", since: float = 0.0,
          limit: int = 50) -> list[RecordRow]:
    """统一查询，按时间倒序；命令侧最多 50 条。"""
    return _filtered(host, group, kinds, target_id, target_text, since)[: max(1, min(50, int(limit or 50)))]


def query_page(host, group: str = "", kinds: Optional[Iterable[str]] = None,
               target_id: str = "", target_text: str = "", since: float = 0.0,
               page: int = 1, per_page: int = 20):
    """分页查询，返回 (当前页记录, 总数)。"""
    rows = _filtered(host, group, kinds, target_id, target_text, since)
    total = len(rows)
    per_page = max(1, min(50, int(per_page or 20)))
    page = max(1, int(page or 1))
    start = (page - 1) * per_page
    return rows[start:start + per_page], total


def get(host, display_id: str, group: str = "") -> Optional[RecordRow]:
    """按 V12/M3/R4/K5 取单条；找不到返回 None。"""
    text = str(display_id or "").strip().upper().lstrip("#")
    if len(text) < 2 or text[0] not in "VMRK":
        return None
    try:
        want_id = int(text[1:])
    except ValueError:
        return None
    kind = {"V": "verdict", "M": "mute", "R": "recall", "K": "kick"}[text[0]]
    for row in iter_rows(host, group=group):
        if row.kind == kind and row.id == want_id:
            return row
    return None
