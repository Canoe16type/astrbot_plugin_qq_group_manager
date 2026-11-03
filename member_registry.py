"""群成员档案：把 member_openid 和「人」对上号。

为什么需要这张表
----------------
QQ 官方机器人 v2 的隐私策略决定了三件事：

1. 群消息事件（GROUP_AT_MESSAGE_CREATE 等）的 author 里**只有 member_openid**，
   没有昵称、没有 QQ 号；
2. 群成员事件（GROUP_MEMBER_ADD / GROUP_MEMBER_REMOVE）同样只有 member_openid，
   另外可能带一个 user_openid（可能为空）；
3. 「获取群成员信息 / 群成员列表」接口有 username 字段，但**目前是内邀白名单**
   （错误码 11253），普通机器人调不到。

于是"退群了却叫不出名字"成了必然结果。本模块的做法是把 openid 当成主键，
维护一张可以**人工补昵称**的档案表：

- 自动记录：谁在群里说过话（openid / 首次出现 / 最后发言 / 发言条数）；
- 人工录入：昵称、备注（指令或网页面板都能改）；
- 接口补全：将来拿到群成员接口权限后，可一键拉取昵称回填（source=api）；
- 退群时：按 openid 查到昵称，通知里就能写「张三 离开了本群」，
  而不是「C3D4E5F6… 离开了本群」。

存储：<插件数据目录>/members.json，结构
    {"groups": {group_openid: {member_openid: {记录}}}}
记录字段见 _new_record()。

写盘策略：发言计数这类高频改动 60 秒最多落一次盘（崩溃最多丢一分钟计数）；
改昵称 / 备注 / 退群标记这类低频且有意义的改动立即落盘。
"""

import json
import os
import time
from typing import Any, Iterable, Optional

DEFAULT_NAME = ""  # 没有昵称时用短标识，不塞"未知成员"这种占位词


def short_id(openid: str, keep: int = 6) -> str:
    """openid -> 便于肉眼区分的短标识，如 #C3D4E5。"""
    s = str(openid or "").strip()
    if not s:
        return "-"
    return "#" + (s[-keep:] if len(s) > keep else s)


def _now() -> float:
    return time.time()


class MemberRegistry:
    """单群成员档案（openid -> 昵称 / 备注 / 活跃度）。"""

    def __init__(self, data_dir: str, logger=None):
        self.dir = data_dir
        self.logger = logger
        self.path = os.path.join(data_dir, "members.json")
        self.groups: dict[str, dict[str, dict]] = {}
        self._dirty = False
        self._last_flush = 0.0
        self._load()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            groups = (data or {}).get("groups") or {}
            if isinstance(groups, dict):
                for g, rows in groups.items():
                    if not isinstance(rows, dict):
                        continue
                    fixed = {}
                    for o, r in rows.items():
                        if not isinstance(r, dict):
                            continue
                        if "union_openid" in r:  # 旧字段名统一成 user_openid
                            r.setdefault("user_openid", r.pop("union_openid") or "")
                            r.pop("union_openid", None)
                        r.setdefault("user_openid", "")
                        fixed[str(o)] = r
                    self.groups[str(g)] = fixed
        except Exception as e:  # noqa: BLE001
            self._log("ERROR", f"读取 members.json 失败，本次以空档案运行: {e}")

    def save(self) -> None:
        """立即落盘（原子替换）。"""
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "groups": self.groups}, f,
                          ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_flush = _now()
        except Exception as e:  # noqa: BLE001
            self._log("ERROR", f"写入 members.json 失败: {e}")

    def maybe_save(self, min_interval: float = 60.0) -> None:
        """高频改动用：距上次落盘不足 min_interval 就攒着。"""
        if not self._dirty:
            return
        if _now() - self._last_flush >= min_interval:
            self.save()

    def _log(self, level: str, msg: str) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level.lower(), self.logger.info)(f"[成员档案] {msg}")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    @staticmethod
    def _new_record(openid: str, ts: float) -> dict:
        return {
            "openid": openid,
            "user_openid": "",      # 用户维度 openid（事件里可能带，可跨群认人）
            "nickname": DEFAULT_NAME,     # 展示用昵称（人工优先）
            "name_source": "none",        # none / manual / api / msg
            "note": "",                   # 备注（比如"活跃老群友""广告号"）
            "role": "",                   # member / admin / owner（有接口才有）
            "first_seen": ts,
            "last_seen": ts,
            "msg_count": 0,
            "status": "active",           # active / left
            "left_at": 0.0,
            "join_count": 1,              # 进群次数（只记次数，不记任何内容）
        }

    def _group(self, group_openid: str, create: bool = False) -> dict[str, dict]:
        g = str(group_openid or "")
        if not g:
            return {}
        if g not in self.groups:
            if not create:
                return {}
            self.groups[g] = {}
        return self.groups[g]

    def get(self, group_openid: str, openid: str) -> Optional[dict]:
        return self._group(group_openid).get(str(openid or ""))

    def seen(
        self,
        group_openid: str,
        openid: str,
        *,
        ts: float | None = None,
        name: str = "",
        user_openid: str = "",
        union_openid: str = "",
        count_msg: bool = True,
    ) -> dict:
        """记录一次「露面」：首次出现 / 最后露面 / 发言数。

        注意 name 只在**没有人工昵称**时才写入，且 source 记为 msg——
        群消息事件其实拿不到昵称，这里只是留了条路（部分适配器/字段
        可能带名字），不能指望它。
        """
        now = float(ts or _now())
        oid = str(openid or "")
        if not str(group_openid or "") or not oid:
            return {}
        # 注意：不能写 `if not rows`——群第一次建档时 rows 就是空 dict，
        # 会被当成"没有这个群"直接返回，导致第一条发言永远记不进去。
        rows = self._group(group_openid, create=True)
        rec = rows.get(oid)
        if rec is None:
            rec = self._new_record(oid, now)
            rows[oid] = rec
            self._dirty = True
            self._log("INFO", f"群 {group_openid} 新成员入档: {short_id(oid)}")
        if rec.get("status") == "left":
            # 说过话就一定在群里（重新入群，或退群事件误报）。
            # 重新入群＝新一轮在群周期：上一条周期留下的统计一律清零，
            # 否则「进群→退群→再进群→再退群」会把两次的数据累加在一起，
            # 退群通知里就会写出早就不是本次的发言数（1.1.1 修复）。
            old_msgs = int(rec.get("msg_count") or 0)
            rec["status"] = "active"
            rec["left_at"] = 0.0
            rec["first_seen"] = now
            rec["last_seen"] = 0.0
            rec["msg_count"] = 0
            rec["join_count"] = int(rec.get("join_count") or 1) + 1
            self._log(
                "INFO",
                f"群 {group_openid} 成员 {short_id(oid)} 重新入群（第 {rec['join_count']} 次），"
                f"发言统计按本轮重新计数（退群时已清零，上轮 {old_msgs} 条仅记录在日志里）",
            )
        rec["last_seen"] = now
        if count_msg:
            rec["msg_count"] = int(rec.get("msg_count") or 0) + 1
        uo = str(user_openid or union_openid or "")
        if uo:
            rec["user_openid"] = uo
        if name and rec.get("name_source") in ("none", "msg", ""):
            rec["nickname"] = str(name).strip()[:40]
            rec["name_source"] = "msg"
        self.maybe_save()
        return rec

    def set_name(self, group_openid: str, openid: str, name: str,
                 source: str = "manual") -> Optional[dict]:
        """改昵称。source=manual 时权限最高，接口回填不会覆盖。"""
        rows = self._group(group_openid)
        rec = rows.get(str(openid or ""))
        if rec is None:
            return None
        rec["nickname"] = str(name or "").strip()[:40]
        rec["name_source"] = source if rec["nickname"] else "none"
        self.save()
        return rec

    def set_note(self, group_openid: str, openid: str, note: str) -> Optional[dict]:
        rec = self._group(group_openid).get(str(openid or ""))
        if rec is None:
            return None
        rec["note"] = str(note or "").strip()[:200]
        self.save()
        return rec

    def mark_left(self, group_openid: str, openid: str,
                  ts: float | None = None, purge_stats: bool = True) -> Optional[dict]:
        """标记退群。没有档案时返回 None（调用方决定是否建"仅有 openid"的记录）。

        purge_stats=True（默认）会把**本轮的活跃统计清零**：发言数、最近发言时间。
        理由：退群＝这一轮在群周期结束，留着的统计既没用、又会在下次进群后
        被误读成"本次数据汇总"。昵称 / 备注 / 进群次数保留——那是人工维护的
        身份信息，删了下次退群就又叫不出名字了。

        注意：一定要在取完统计（发退群通知用）之后再调用，否则通知里的
        「共发言 N 条」会变成 0。
        """
        rec = self._group(group_openid).get(str(openid or ""))
        if rec is None:
            return None
        rec["status"] = "left"
        rec["left_at"] = float(ts or _now())
        if purge_stats:
            rec["msg_count"] = 0
            rec["last_seen"] = 0.0
        self.save()
        return rec

    def create_left(self, group_openid: str, openid: str,
                    ts: float | None = None) -> dict:
        """退群但档案里没这个人：建一条只有 openid 的记录，便于事后追溯。"""
        rec = self.seen(group_openid, openid, ts=ts, count_msg=False)
        if not rec:
            return {}
        rec["status"] = "left"
        rec["left_at"] = float(ts or _now())
        self.save()
        return rec

    def name_by_user_openid(self, user_openid: str, exclude_group: str = "") -> tuple[str, str]:
        """跨群认人：用 user_openid 在其它群里找已经补过的名字。

        群成员事件可能带 user_openid（同一机器人的用户维度 ID，跨群稳定），
        所以 A 群补过名字，B 群退群时也能叫出名字。返回 (昵称, 来源群)。
        只认 manual / api 这类可信来源，msg 里的名字照样可以用，但不优先。
        """
        uo = str(user_openid or "").strip()
        if not uo:
            return "", ""
        best = ("", "")
        for g, rows in self.groups.items():
            if g == exclude_group:
                continue
            for rec in rows.values():
                if str(rec.get("user_openid") or "") != uo:
                    continue
                name = str(rec.get("nickname") or "").strip()
                if not name:
                    continue
                if rec.get("name_source") in ("manual", "api"):
                    return name, g
                if not best[0]:
                    best = (name, g)
        return best

    def bind_user_openid(self, group_openid: str, openid: str, user_openid: str) -> None:
        """把事件里带的 user_openid 记到档案上（供日后跨群认人）。"""
        rec = self.get(group_openid, openid)
        if rec is None or not user_openid:
            return
        if str(rec.get("user_openid") or "") != str(user_openid):
            rec["user_openid"] = str(user_openid)
            self.save()

    def label(self, group_openid: str, openid: str) -> str:
        """展示名：昵称 > 短标识。"""
        rec = self.get(group_openid, openid) or {}
        name = str(rec.get("nickname") or "").strip()
        return name or short_id(openid)

    def label_with_id(self, group_openid: str, openid: str) -> str:
        """展示名（带短标识，避免重名歧义）。"""
        rec = self.get(group_openid, openid) or {}
        name = str(rec.get("nickname") or "").strip()
        return f"{name}（{short_id(openid)}）" if name else short_id(openid)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_members(
        self,
        group_openid: str,
        *,
        status: str = "all",
        keyword: str = "",
        sort: str = "last_seen",
        offset: int = 0,
        limit: int = 0,
    ) -> tuple[list[dict], int]:
        """列表 + 分页。status: all / active / left / named / unnamed。"""
        rows = list(self._group(group_openid).values())
        kw = str(keyword or "").strip().lower()
        if status == "active":
            rows = [r for r in rows if r.get("status") != "left"]
        elif status == "left":
            rows = [r for r in rows if r.get("status") == "left"]
        elif status == "named":
            rows = [r for r in rows if str(r.get("nickname") or "").strip()]
        elif status == "unnamed":
            rows = [r for r in rows if not str(r.get("nickname") or "").strip()]
        if kw:
            def hit(r: dict) -> bool:
                return any(
                    kw in str(r.get(k) or "").lower()
                    for k in ("nickname", "note", "openid", "user_openid")
                )
            rows = [r for r in rows if hit(r)]
        key = {
            "last_seen": lambda r: -(float(r.get("last_seen") or 0)),
            "first_seen": lambda r: -(float(r.get("first_seen") or 0)),
            "msg_count": lambda r: -(int(r.get("msg_count") or 0)),
            "name": lambda r: str(r.get("nickname") or "\uffff"),
            "left_at": lambda r: -(float(r.get("left_at") or 0)),
        }.get(sort, lambda r: -(float(r.get("last_seen") or 0)))
        # 在群的人永远排在已退群的人前面（同组内再按所选字段排），
        # 否则「最近发言」会把刚退群的排到最上面，看着像还在群里。
        rows.sort(key=lambda r: (1 if r.get("status") == "left" else 0, key(r)))
        total = len(rows)
        if limit and limit > 0:
            rows = rows[offset: offset + limit]
        elif offset:
            rows = rows[offset:]
        return rows, total

    def stats(self, group_openid: str) -> dict:
        rows = list(self._group(group_openid).values())
        active = sum(1 for r in rows if r.get("status") != "left")
        named = sum(1 for r in rows if str(r.get("nickname") or "").strip())
        talked = sum(1 for r in rows if int(r.get("msg_count") or 0) > 0)
        return {
            "total": len(rows),
            "active": active,
            "left": len(rows) - active,
            "named": named,
            "unnamed": len(rows) - named,
            "talked": talked,
            "messages": sum(int(r.get("msg_count") or 0) for r in rows),
        }

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def delete(self, group_openid: str, openid: str) -> bool:
        rows = self._group(group_openid)
        if str(openid or "") in rows:
            rows.pop(str(openid or ""), None)
            self.save()
            return True
        return False

    def delete_left(self, group_openid: str) -> int:
        rows = self._group(group_openid)
        gone = [o for o, r in rows.items() if r.get("status") == "left"]
        for o in gone:
            rows.pop(o, None)
        if gone:
            self.save()
        return len(gone)

    def upsert_many(self, group_openid: str, rows: Iterable[dict]) -> tuple[int, int]:
        """批量导入（面板 / 将来群成员列表接口共用）。

        rows: [{openid, nickname?, note?, role?, user_openid?, joined_at?}]
        返回 (新增, 更新)。接口回填不覆盖人工昵称。
        """
        added = updated = 0
        grp = self._group(group_openid, create=True)
        for row in rows or []:
            oid = str((row or {}).get("openid") or (row or {}).get("member_openid") or "").strip()
            if not oid:
                continue
            rec = grp.get(oid)
            if rec is None:
                rec = self._new_record(oid, _now())
                grp[oid] = rec
                added += 1
            else:
                updated += 1
            name = str((row or {}).get("nickname") or (row or {}).get("username") or "").strip()
            if name and rec.get("name_source") != "manual":
                rec["nickname"] = name[:40]
                rec["name_source"] = "api"
            if (row or {}).get("note"):
                rec["note"] = str(row["note"]).strip()[:200]
            if (row or {}).get("role"):
                rec["role"] = str(row["role"])
            uo = str((row or {}).get("user_openid") or (row or {}).get("union_openid") or "").strip()
            if uo:
                rec["user_openid"] = uo
            if (row or {}).get("joined_at"):
                rec["joined_at"] = str(row["joined_at"])
            if (row or {}).get("status") in ("active", "left"):
                rec["status"] = row["status"]
        self.save()
        return added, updated

    def has(self, group_openid: str, openid: str) -> bool:
        return str(openid or "") in self._group(group_openid)
