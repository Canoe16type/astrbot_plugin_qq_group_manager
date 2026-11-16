"""状态：日报构建/发送状态、群轮询与活跃度。"""

from __future__ import annotations

import asyncio
import json
import re
import time

from ...kernel.timefmt import active_timezone_name


def _load_report_state(host):
        """读取上次日报日期。

        内存标记只在本次进程内有效，而 AstrBot 在 WebUI 里保存配置、重载插件
        都会重建插件实例 —— 只要当天日报已经发过，重启后 30 秒内又会再发一遍，
        群里就是重复推送。所以把日期落盘。
        """
        try:
            with open(host._report_state_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            day = str((data or {}).get("last_day") or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                host._report_last_day = day
        except FileNotFoundError:
            return
        except Exception as e:  # 坏了不影响功能，当今天没发过
            host.logger.warning(f"[日报] 状态读取失败: {e}")

async def _track_group_activity(host, event):
        """群消息的记账动作：昵称缓存 → 成员档案 → 轮询/注入兜底 → 消息缓存 → 巡检 → 新群欢迎。"""
        g = event.get_group_id()
        if g:
            sid = event.get_sender_id()
            sname = event.get_sender_name()
            if sid and sname:
                host._nick_cache.setdefault(g, {})[sid] = sname
            host._record_member_seen(event)
        host._ensure_poll_task()
        host._ensure_member_event_injected()  # 兜底：平台实例就绪后重试注入
        host._record_recent_msg(event)        # 引用撤回：原文匹配需要消息缓存
        await host._maybe_auto_scan(event)    # 审核：仅当「本群」开了巡检开关才会执行
        await host._maybe_welcome_new_group(event)

def _report_options(host):
        """(是否开启, (时, 分), 目标群 openid)。配置非法时逐项兜底，不抛异常。"""
        enabled = bool(host._read_config_value(
            ("approval", "report_enabled"), default=False, legacy_keys=("report_enabled",),
        ))
        raw_time = str(host._read_config_value(
            ("approval", "report_time"), default="09:00", legacy_keys=("report_time",),
        ) or "").strip()
        hh, mm = 9, 0
        m = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", raw_time)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                hh, mm = h, mi
            else:
                host.logger.warning(f"[日报] 汇报时间 {raw_time!r} 超出范围，按 09:00 处理")
        elif raw_time:
            host.logger.warning(f"[日报] 汇报时间 {raw_time!r} 格式不对（要 HH:MM），按 09:00 处理")
        target = str(host._read_config_value(
            ("approval", "report_group"), default="", legacy_keys=("report_group",),
        ) or "").strip()
        return enabled, (hh, mm), target

def _report_candidates(host, target):
        """定时汇报要发到哪些群（按群逐个汇报，不再只挑一个群）。

        - 留空（默认）＝**所有已知群**，各自群里有待处理申请就发到各自群；
        - 填了＝只汇报匹配到的群，支持逗号 / 空格分隔多个 openid 或群名，
          适合「只想让主群收日报」的场景。
        """
        if not target:
            return list(host._known_groups.items())
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for token in re.split(r"[,，;；\s]+", target):
            token = token.strip()
            if token:
                hit = host._match_group(token)
                if hit is None:
                    host.logger.warning(
                        f"[日报] 汇报群 {token!r} 既不是已知 openid 也匹配不到群名，已跳过"
                    )
                    continue
                if hit[0] not in seen:
                    seen.add(hit[0])
                    out.append(hit)
        return out

def _ensure_report_task(host):
        """确保日报任务在跑。WebUI 里刚打开开关时也能立刻生效（不用重载插件）。"""
        if not host._report_options()[0]:
            return
        if host._report_task is None or host._report_task.done():
            host._report_task = asyncio.create_task(host._report_loop())
            enabled, (hh, mm), target = host._report_options()
            host.logger.info(
                f"[日报] 定时汇报任务启动：每天 {hh:02d}:{mm:02d}（时区 {active_timezone_name()}），"
                f"汇报范围={target or '所有已知群（按群逐个汇报）'}"
            )

async def _report_loop(host):
        while True:
            try:
                await host._maybe_send_report()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                host.logger.error(f"[日报] 汇报任务异常: {e}")
            await asyncio.sleep(30)

async def _build_report_text(host, group_openid, platform_id, *, pending=None):
        """拼日报正文：待处理申请（含名单）+ 当前禁言人数。

        Args:
            pending: 已经拉好的待处理列表（定时汇报会先拉一次用于判断「要不要发」），
                不传则内部自己拉。
        """
        now = host._local_now()
        if pending is None:
            try:
                pending = await host._refresh_requests(group_openid, platform_id)
            except Exception as e:  # noqa: BLE001
                host.logger.warning(f"[日报] 拉取待处理申请失败，改用缓存: {e}")
                pending = host._pending_list(group_openid)
        if pending:
            limit = 10
            lines = [host._fmt_request_line(i + 1, r) for i, r in enumerate(pending[:limit])]
            if len(pending) > limit:
                lines.append(f"…另有 {len(pending) - limit} 条，发 /群管 审批 全部 查看")
            text_list = "\n".join(lines)
        else:
            text_list = "暂无待处理申请 ✅"

        muted = 0
        try:
            data = await host._get_api(platform_id).get_mute_state(group_openid)
            muted = len(data.get("members") or [])
        except Exception as e:  # noqa: BLE001
            host.logger.debug(f"[日报] 查询禁言人数失败（按 0 计）: {e}")

        meta = host.mod.get_group_meta(group_openid)
        return str(host._render(
            "msg_report_daily",
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H:%M"),
            pending_count=len(pending),
            list=text_list,
            muted_count=muted,
            group=str(meta.get("name") or "") or group_openid[-6:],
        ))

async def _poll_loop(host):
        mode = host._join_event_mode()
        host.logger.info(
            "入群申请轮询任务启动"
            f"（模式={mode}：event_first=事件为主+低频兜底，interval=纯轮询，off=关闭）"
        )
        while True:
            mode = host._join_event_mode()
            try:
                if mode == "off":
                    # 关闭轮询：只保留事件通道。申请仍可通过 /群管 审批 指令即时拉取。
                    await asyncio.sleep(300)
                    continue
                groups = host._managed_groups()
                if not groups:
                    host.logger.debug("轮询: 暂无可管理群，跳过")
                for g, pid in groups.items():
                    try:
                        await host._poll_group(g, pid)
                    except Exception as e:
                        host.logger.error(f"轮询群 {g} 失败: {e}")
            except Exception as e:
                host.logger.error(f"轮询循环异常: {e}")
            await asyncio.sleep(host._poll_interval_seconds())

def _poll_interval_seconds(host):
        """本轮轮询间隔（秒）。

        - ``interval``：用户配的 ``poll_interval``（老行为，默认 30s）；
        - ``event_first``：读 ``poll_interval_fallback``（默认 300s），下限 60s。
          事件通道已经把「即时性」接走了，轮询只负责「事件漏推时补一刀」，
          没必要继续 30 秒一次去撞 30 QPM 的接口频控。
        """
        mode = host._join_event_mode()
        if mode == "event_first":
            raw = host._read_config_value(
                ("approval", "poll_interval_fallback"), default=300,
                legacy_keys=("poll_interval_fallback",),
            )
            try:
                return max(60, int(raw))
            except (TypeError, ValueError):
                return 300
        raw = host._read_config_value(
            ("approval", "poll_interval"), default=30, legacy_keys=("poll_interval",)
        )
        try:
            return max(10, int(raw))
        except (TypeError, ValueError):
            return 30

async def _poll_group(host, group_openid, platform_id):
        # 用轮询任务自己维护的 _seen_requests 判断新申请，而不是拿共享的
        # host._pending 对比（它会被 /审批 指令、WebUI /requests 接口更新，
        # 导致申请被提前"见过"、通知永远发不出去）。
        seen = host._seen_requests.get(group_openid) or set()
        if time.time() < host._join_backoff.get(group_openid, 0.0):
            return
        items = await host._refresh_requests(group_openid, platform_id)
        if host._join_error_code.get(group_openid) == 11703:
            # 机器人不是群管理员：退避 30 分钟，且只提示一次，不再刷日志。
            host._join_backoff[group_openid] = time.time() + 1800
            if group_openid not in host._join_denied_logged:
                host._join_denied_logged.add(group_openid)
                host.logger.warning(
                    f"群 {group_openid} 机器人不是群管理员，入群申请通知不可用；"
                    "已改为每 30 分钟重试一次，重试成功会自动恢复"
                )
            return
        host._join_backoff.pop(group_openid, None)
        host._join_denied_logged.discard(group_openid)
        new = [r for r in items if host._req_key(r) not in seen]
        host._seen_requests[group_openid] = {host._req_key(r) for r in items}
        if not new:
            return
        host.logger.info(f"群 {group_openid} 轮询发现 {len(new)} 个入群申请")
        await host._notify_join_requests(group_openid, platform_id, new, source="轮询")
