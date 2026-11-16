"""日志与状态 Web API（SSE 心跳常量也在这里）。"""

from __future__ import annotations

import asyncio
import json
import traceback
import traceback

from astrbot.api.web import json_response, request, stream_response

from ...kernel.constants import PLUGIN_NAME, PLUGIN_VERSION
from ...kernel.timefmt import fmt_ts

from ...kernel.constants import PLUGIN_NAME, PLUGIN_VERSION
from ...kernel.timefmt import fmt_ts

LOG_SSE_HEARTBEAT_SEC = 15  # 日志 SSE 空闲保活间隔（秒）


async def web_logs(host):
        level = request.query.get("level", type=str) or "ALL"
        keyword = request.query.get("keyword", type=str) or ""
        limit = request.query.get("limit", 200, type=int)
        return json_response({"logs": host.logger.search(level, keyword, limit),
                              **host.logger.log_status()})

async def web_logs_stream(host):
        """SSE 日志流。

        没有新日志时必须定期发一行 SSE 注释（`: keepalive`）保活：
        否则连接会长时间零字节，被 Nginx / WebUI 反代按空闲超时断开，
        前端就表现为「断开几十秒」。注释行会被 EventSource 忽略，不影响数据。
        """
        async def gen():
            idx = host.logger.last_index
            idle = 0
            try:
                while True:
                    entries = host.logger.get_since(idx)
                    for e in entries:
                        yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                        idx = e["index"]
                    if entries:
                        idle = 0
                    else:
                        idle += 1
                        if idle >= LOG_SSE_HEARTBEAT_SEC:
                            idle = 0
                            yield ": keepalive\n\n"
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass

        return stream_response(gen())

async def web_logs_clear(host):
        """清空内存日志，并删除磁盘上的日志文件（真正释放空间）。"""
        n = host.logger.clear(remove_files=True)
        host.logger.info(f"WebUI 清空日志：内存 {n} 条 + 磁盘文件已删除")
        return json_response({"cleared": n, **host.logger.log_status()})


async def web_status(host):
        # 面板上所有时间都按这个时区渲染（前端 chip / 脚注直接用这个值）
        timezone_name = host._refresh_timezone()
        groups = host._group_entries()
        return json_response({
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "timezone": timezone_name,
            "platforms": host._platform_list(),
            "features": {
                "enable_mute": bool(host._read_config_value(("basic", "enable_mute"), default=True, legacy_keys=("enable_mute",))),
                "enable_approval": bool(host._read_config_value(("approval", "enable_approval"), default=True, legacy_keys=("enable_approval",))),
                "enable_join_request_notify": bool(host._read_config_value(("approval", "enable_join_request_notify"), default=True, legacy_keys=("enable_join_request_notify",))),
                "enable_welcome": bool(host._read_config_value(("basic", "enable_welcome"), default=True, legacy_keys=("enable_welcome",))),
                "enable_group_add_intro": bool(host._read_config_value(("basic", "enable_group_add_intro"), default=True, legacy_keys=("enable_group_add_intro",))),
                "enable_new_group_welcome": bool(host._read_config_value(("basic", "enable_new_group_welcome"), default=True, legacy_keys=("enable_new_group_welcome",))),
                "enable_review": bool(host._read_config_value(("moderation", "enable"), default=True, legacy_keys=("moderation_enable",))),
                "enable_recall": bool(host._read_config_value(("recall", "enable_recall"), default=True, legacy_keys=("enable_recall",))),
                "enable_member_remove_notify": bool(host._read_config_value(("member_event", "enable_member_remove_notify"), default=False, legacy_keys=("enable_member_remove_notify",))),
                "enable_member_registry": bool(host._read_config_value(("member_registry", "enabled"), default=True, legacy_keys=("member_registry_enabled",))),
            },
            "admin": {
                "mode": str(host._read_config_value(("admin", "admin_mode"), default="any", legacy_keys=("admin_mode",))),
                "use_astrbot_admin": bool(host._read_config_value(("admin", "admin_use_astrbot_admin"), default=True, legacy_keys=("admin_use_astrbot_admin",))),
                "use_qq_role": bool(host._read_config_value(("admin", "admin_use_qq_role"), default=True, legacy_keys=("admin_use_qq_role",))),
                "extra_openids": host._read_config_value(("admin", "admin_openids"), default=[], legacy_keys=("admin_openids",)) or [],
            },
            "target_groups": host._read_config_value(("basic", "target_group_openids"), default=[], legacy_keys=("target_group_openids",)) or [],
            "known_groups": groups,
            "pending_count": sum(len(v) for v in host._pending.values()),
            "poll_interval": int(host._read_config_value(("approval", "poll_interval"), default=30, legacy_keys=("poll_interval",)) or 30),
            "poll_running": bool(host._poll_task and not host._poll_task.done()),
            # 1.4.0：申请获取方式（事件为主 / 纯轮询 / 仅事件）与按钮通道状态
            "approval": {
                "join_event_mode": host._join_event_mode(),
                "poll_interval_effective": host._poll_interval_seconds(),
                "poll_interval_fallback": int(host._read_config_value(("approval", "poll_interval_fallback"), default=300, legacy_keys=("poll_interval_fallback",)) or 300),
                "buttons": {
                    "enabled": bool(host._join_button_options()[0]),
                    "channel_ready": host._button_channel_ready(),
                    "permission": host._join_button_options()[1],
                    "max_requests": host._join_button_options()[2],
                },
                "join_event": {
                    "total": host._join_event_total,
                    "last_at": host._join_event_last,
                    "last_text": fmt_ts(host._join_event_last) if host._join_event_last else "",
                    # 有群收到过事件 = 事件通道确实在推；一个都没收到多半是
                    # 机器人不是群管理员（平台不推），或者注入后还没重连完成
                    "groups_seen": len(host._join_event_at),
                },
                "interaction": {
                    "total": host._interaction_total,
                    "last_at": host._interaction_last,
                    "last_text": fmt_ts(host._interaction_last) if host._interaction_last else "",
                    "last_result": host._interaction_last_result,
                },
            },
            "log": {
                "level": str(host._read_config_value(("log", "log_level"), default="DEBUG", legacy_keys=("log_level",))),
                "entries": host.logger.last_index + 1,
                "file": host.logger.file_path,
            },
        })

async def web_requests(host):
        """GET /requests?group_openid=xxx 待处理入群申请。

        不传（或传 all）＝汇总全部已知群的申请（每条带所属群，便于分群查看）；
        传具体群＝只刷新并展示那一个群。_refresh_requests 内部失败会退回本地缓存。

        最外层再包一层兜底：面板这一块宁可显示「哪一步挂了」，也不要整块
        「加载失败: Internal server error」——500 一到前端就只剩一句通用报错，
        排查时完全看不出是哪一步炸的（历史现象：群选择器/群名元数据坏掉也会 500）。
        """
        try:
            return await host._web_requests_impl()
        except Exception as ex:  # noqa: BLE001
            import traceback

            tb = traceback.format_exc()
            host.logger.error(f"[/requests] 查询待审批列表失败: {ex}\n{tb}")
            return json_response({
                "error": "requests_failed",
                "message": str(ex) or ex.__class__.__name__,
                "scope": "all",
                "group_openid": "",
                "platform_id": "",
                "group_display": "",
                "groups": [],
                "selected": [],
                "requests": [],
                "errors": [{"group_openid": "", "group_display": "全部", "bot": "",
                            "error": str(ex) or ex.__class__.__name__}],
            })

async def web_poll_now(host):
        groups = host._managed_groups()
        results = {}
        for g, pid in groups.items():
            try:
                await host._poll_group(g, pid)
                results[g] = "ok"
            except Exception as e:
                results[g] = str(e)
        return json_response({"groups": results})


async def web_status(host):
        # 面板上所有时间都按这个时区渲染（前端 chip / 脚注直接用这个值）
        timezone_name = host._refresh_timezone()
        groups = host._group_entries()
        return json_response({
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "timezone": timezone_name,
            "platforms": host._platform_list(),
            "features": {
                "enable_mute": bool(host._read_config_value(("basic", "enable_mute"), default=True, legacy_keys=("enable_mute",))),
                "enable_approval": bool(host._read_config_value(("approval", "enable_approval"), default=True, legacy_keys=("enable_approval",))),
                "enable_join_request_notify": bool(host._read_config_value(("approval", "enable_join_request_notify"), default=True, legacy_keys=("enable_join_request_notify",))),
                "enable_welcome": bool(host._read_config_value(("basic", "enable_welcome"), default=True, legacy_keys=("enable_welcome",))),
                "enable_group_add_intro": bool(host._read_config_value(("basic", "enable_group_add_intro"), default=True, legacy_keys=("enable_group_add_intro",))),
                "enable_new_group_welcome": bool(host._read_config_value(("basic", "enable_new_group_welcome"), default=True, legacy_keys=("enable_new_group_welcome",))),
                "enable_review": bool(host._read_config_value(("moderation", "enable"), default=True, legacy_keys=("moderation_enable",))),
                "enable_recall": bool(host._read_config_value(("recall", "enable_recall"), default=True, legacy_keys=("enable_recall",))),
                "enable_member_remove_notify": bool(host._read_config_value(("member_event", "enable_member_remove_notify"), default=False, legacy_keys=("enable_member_remove_notify",))),
                "enable_member_registry": bool(host._read_config_value(("member_registry", "enabled"), default=True, legacy_keys=("member_registry_enabled",))),
            },
            "admin": {
                "mode": str(host._read_config_value(("admin", "admin_mode"), default="any", legacy_keys=("admin_mode",))),
                "use_astrbot_admin": bool(host._read_config_value(("admin", "admin_use_astrbot_admin"), default=True, legacy_keys=("admin_use_astrbot_admin",))),
                "use_qq_role": bool(host._read_config_value(("admin", "admin_use_qq_role"), default=True, legacy_keys=("admin_use_qq_role",))),
                "extra_openids": host._read_config_value(("admin", "admin_openids"), default=[], legacy_keys=("admin_openids",)) or [],
            },
            "target_groups": host._read_config_value(("basic", "target_group_openids"), default=[], legacy_keys=("target_group_openids",)) or [],
            "known_groups": groups,
            "pending_count": sum(len(v) for v in host._pending.values()),
            "poll_interval": int(host._read_config_value(("approval", "poll_interval"), default=30, legacy_keys=("poll_interval",)) or 30),
            "poll_running": bool(host._poll_task and not host._poll_task.done()),
            # 1.4.0：申请获取方式（事件为主 / 纯轮询 / 仅事件）与按钮通道状态
            "approval": {
                "join_event_mode": host._join_event_mode(),
                "poll_interval_effective": host._poll_interval_seconds(),
                "poll_interval_fallback": int(host._read_config_value(("approval", "poll_interval_fallback"), default=300, legacy_keys=("poll_interval_fallback",)) or 300),
                "buttons": {
                    "enabled": bool(host._join_button_options()[0]),
                    "channel_ready": host._button_channel_ready(),
                    "permission": host._join_button_options()[1],
                    "max_requests": host._join_button_options()[2],
                },
                "join_event": {
                    "total": host._join_event_total,
                    "last_at": host._join_event_last,
                    "last_text": fmt_ts(host._join_event_last) if host._join_event_last else "",
                    # 有群收到过事件 = 事件通道确实在推；一个都没收到多半是
                    # 机器人不是群管理员（平台不推），或者注入后还没重连完成
                    "groups_seen": len(host._join_event_at),
                },
                "interaction": {
                    "total": host._interaction_total,
                    "last_at": host._interaction_last,
                    "last_text": fmt_ts(host._interaction_last) if host._interaction_last else "",
                    "last_result": host._interaction_last_result,
                },
            },
            "log": {
                "level": str(host._read_config_value(("log", "log_level"), default="DEBUG", legacy_keys=("log_level",))),
                "entries": host.logger.last_index + 1,
                "file": host.logger.file_path,
            },
        })

async def web_requests(host):
        """GET /requests?group_openid=xxx 待处理入群申请。

        不传（或传 all）＝汇总全部已知群的申请（每条带所属群，便于分群查看）；
        传具体群＝只刷新并展示那一个群。_refresh_requests 内部失败会退回本地缓存。

        最外层再包一层兜底：面板这一块宁可显示「哪一步挂了」，也不要整块
        「加载失败: Internal server error」——500 一到前端就只剩一句通用报错，
        排查时完全看不出是哪一步炸的（历史现象：群选择器/群名元数据坏掉也会 500）。
        """
        try:
            return await host._web_requests_impl()
        except Exception as ex:  # noqa: BLE001
            import traceback

            tb = traceback.format_exc()
            host.logger.error(f"[/requests] 查询待审批列表失败: {ex}\n{tb}")
            return json_response({
                "error": "requests_failed",
                "message": str(ex) or ex.__class__.__name__,
                "scope": "all",
                "group_openid": "",
                "platform_id": "",
                "group_display": "",
                "groups": [],
                "selected": [],
                "requests": [],
                "errors": [{"group_openid": "", "group_display": "全部", "bot": "",
                            "error": str(ex) or ex.__class__.__name__}],
            })

async def web_poll_now(host):
        groups = host._managed_groups()
        results = {}
        for g, pid in groups.items():
            try:
                await host._poll_group(g, pid)
                results[g] = "ok"
            except Exception as e:
                results[g] = str(e)
        return json_response({"groups": results})
