"""日志与状态 Web API（SSE 心跳常量也在这里）。"""

from __future__ import annotations

import asyncio
import json

from astrbot.api.web import json_response, request, stream_response

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
