"""QQ 官方机器人 API 封装：access_token 管理 + 群管理 REST 接口调用。

文档参考:
- 获取凭证: https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html
- 设置群成员禁言: POST /v2/groups/{group_openid}/restrict_chat_setting
- 查询群禁言状态: GET  /v2/groups/{group_openid}/restrict_chat_setting
- 入群申请列表:   GET  /v2/groups/{group_openid}/join_request_list
- 入群申请审批:   POST /v2/groups/{group_openid}/approval_join_request/{member_openid}
- 获取群信息:     GET  /v2/groups/{group_openid}/info
- 主动发群消息:   POST /v2/groups/{group_openid}/messages
- 撤回群消息:     DELETE /v2/groups/{group_openid}/messages/{message_id}
"""

import asyncio
import json
import re
import time

import aiohttp

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# 日志脱敏用：这些字段的值一律打码，不落日志文件
_SCRUB_KEYS = ("access_token", "clientSecret", "client_secret", "secret")
_SCRUB_RE = re.compile(
    r'"(?:' + "|".join(re.escape(k) for k in _SCRUB_KEYS) + r')"\s*:\s*"[^"]*"',
    re.I,
)


def scrub_secrets(text: str, limit: int = 500) -> str:
    """把疑似凭据的 JSON 字段值替换成 ***，并截断到 limit 字符。"""
    s = _SCRUB_RE.sub('"***": "***"', str(text or ""))
    return s[:limit]


class QQApiError(Exception):
    """QQ 接口调用错误。"""

    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(message)

    def __str__(self) -> str:
        parts = []
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        parts.append(self.args[0])
        return " ".join(parts)


class QQApi:
    """面向单个机器人（appid+secret）的 API 客户端。"""

    def __init__(self, appid: str, secret: str, logger=None, proxy: str | None = None):
        self.appid = str(appid)
        self.secret = secret
        self.logger = logger
        self.proxy = proxy
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._seq = 0

    # ---------- 会话管理 ----------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- Token ----------
    async def get_access_token(self) -> str:
        """获取（或复用缓存的）access_token，提前 60s 过期刷新。"""
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token
        async with self._token_lock:
            if self._token and time.time() < self._token_expire_at - 60:
                return self._token
            self._log("DEBUG", f"[token] 请求新 access_token (appid={self.appid})")
            session = await self._get_session()
            try:
                async with session.post(
                    TOKEN_URL,
                    json={"appId": self.appid, "clientSecret": self.secret},
                ) as resp:
                    body = await resp.text()
                    self._log("DEBUG", f"[token] HTTP {resp.status} resp={scrub_secrets(body)}")
                    if resp.status != 200:
                        raise QQApiError(f"获取 access_token 失败: HTTP {resp.status} {scrub_secrets(body, 200)}", status=resp.status)
                    data = json.loads(body)
                    token = data.get("access_token")
                    if not token:
                        raise QQApiError(f"access_token 响应异常: {scrub_secrets(body, 200)}")
                    self._token = token
                    self._token_expire_at = time.time() + int(data.get("expires_in", 7200))
                    self._log("INFO", f"[token] access_token 获取成功，有效期 {data.get('expires_in', 7200)}s")
                    return token
            except aiohttp.ClientError as e:
                raise QQApiError(f"请求 token 接口失败: {e}") from e

    # ---------- 通用请求 ----------
    async def _request(self, method: str, path: str, params: dict | None = None,
                       body: dict | None = None) -> dict:
        token = await self.get_access_token()
        session = await self._get_session()
        url = f"{API_BASE}{path}"
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        }
        t0 = time.time()
        self._log(
            "DEBUG",
            f"[api] {method} {path} params={json.dumps(params or {}, ensure_ascii=False)[:300]} "
            f"body={json.dumps(body or {}, ensure_ascii=False)[:500]}",
        )
        try:
            async with session.request(method, url, params=params, json=body, headers=headers) as resp:
                text = await resp.text()
                cost = (time.time() - t0) * 1000
                self._log(
                    "DEBUG",
                    f"[api] {method} {path} -> HTTP {resp.status} 耗时{cost:.0f}ms resp={text[:800]}",
                )
                if resp.status == 204 or not text:
                    return {}
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    if resp.status >= 400:
                        raise QQApiError(f"接口返回非 JSON: {text[:200]}", status=resp.status)
                    return {"raw": text}
                if resp.status >= 400:
                    code = data.get("code")
                    message = data.get("message") or data.get("msg") or text[:200]
                    raise QQApiError(f"{message}", code=code, status=resp.status)
                return data
        except aiohttp.ClientError as e:
            self._log("ERROR", f"[api] {method} {path} 网络错误: {e}")
            raise QQApiError(f"网络请求失败: {e}") from e

    def _log(self, level: str, msg: str) -> None:
        if self.logger is not None:
            getattr(self.logger, level.lower())(msg)

    # ---------- 群禁言 ----------
    async def set_mute(self, group_openid: str, member_openid: str,
                       mute_expire_at: str | None = None, op: str = "add") -> dict:
        """设置/更新/解除禁言。op: add / update / del。"""
        item: dict = {"op": op, "member_openid": member_openid}
        if mute_expire_at:
            item["mute_expire_at"] = mute_expire_at
        return await self._request(
            "POST",
            f"/v2/groups/{group_openid}/restrict_chat_setting",
            body={"members": [item]},
        )

    async def get_mute_state(self, group_openid: str) -> dict:
        """查询群禁言状态，返回 {'global_rule': ..., 'members': [...]}。"""
        return await self._request(
            "GET", f"/v2/groups/{group_openid}/restrict_chat_setting"
        )

    # ---------- 入群申请 ----------
    async def get_join_request_list(self, group_openid: str,
                                    cursor: str = "", limit: int = 20) -> dict:
        """拉取入群申请列表。返回 {'list': [...], 'next_cursor': ...}。"""
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", f"/v2/groups/{group_openid}/join_request_list", params=params
        )

    async def approve_join_request(self, group_openid: str, member_openid: str,
                                   join_request_id: str, op: str = "approve",
                                   reject_reason: str | None = None,
                                   add_to_blacklist: bool = False) -> dict:
        """审批入群申请。op: approve / decline。"""
        body: dict = {"op": op, "join_request_id": join_request_id}
        if op == "decline":
            if reject_reason:
                body["reject_reason"] = reject_reason
            if add_to_blacklist:
                body["add_to_member_blacklist"] = True
        return await self._request(
            "POST",
            f"/v2/groups/{group_openid}/approval_join_request/{member_openid}",
            body=body,
        )

    # ---------- 群信息 ----------
    async def fetch_group_info(self, group_openid: str) -> tuple[dict | None, "QQApiError | None"]:
        """拉群基本信息，返回 (数据, 错误)。

        保留错误的 code，便于上层区分 11253（接口仅白名单机器人可用）。
        文档: GET /v2/groups/{group_openid}/info（30 QPM）
        """
        try:
            data = await self._request("GET", f"/v2/groups/{group_openid}/info")
            return (data if isinstance(data, dict) else None), None
        except QQApiError as e:
            return None, e

    async def get_bot_info(self) -> dict | None:
        """获取机器人自身详情（username/avatar），普通接口，失败返回 None。

        文档: GET /users/@me（50 QPS）
        """
        try:
            data = await self._request("GET", "/users/@me")
            return data if isinstance(data, dict) else None
        except QQApiError as e:
            self._log("DEBUG", f"[api] 获取机器人信息失败: {e}")
            return None

    async def get_group_info(self, group_openid: str) -> dict | None:
        """获取群基本信息（可能为内邀接口，失败返回 None 不抛出）。"""
        try:
            return await self._request("GET", f"/v2/groups/{group_openid}/info")
        except QQApiError as e:
            self._log("WARNING", f"[api] 获取群信息失败（可能无权限）: {e}")
            return None

    # ---------- 群成员信息 ----------
    async def get_group_member(self, group_openid: str, member_openid: str) -> dict | None:
        """获取群成员信息（昵称/头像等）。

        注意：成员退群/被移出后该接口大概率查不到（404），失败返回 None 不抛出。
        文档: GET /v2/groups/{group_openid}/members/{openid}
        """
        try:
            return await self._request(
                "GET", f"/v2/groups/{group_openid}/members/{member_openid}"
            )
        except QQApiError as e:
            self._log("DEBUG", f"[api] 获取群成员信息失败（可能已退群）: {e}")
            return None

    # ---------- 群成员列表（内邀能力，为将来开放预留） ----------
    async def list_group_members(self, group_openid: str,
                                 max_pages: int = 20) -> list[dict]:
        """拉取群成员列表（含 username 昵称），自动按 next_cursor 翻页。

        文档: GET /v2/groups/{group_openid}/members?cursor=...
        每次最多 30 条；**当前是内邀白名单能力**，普通机器人会收到
        11253（应用无接口访问权限），此时抛 QQApiError，由调用方提示用户。
        """
        out: list[dict] = []
        cursor = ""
        for _ in range(max(1, max_pages)):
            data = await self._request(
                "GET",
                f"/v2/groups/{group_openid}/members",
                params={"cursor": cursor} if cursor else None,
            )
            if not isinstance(data, dict):
                break
            batch = data.get("members") or []
            if isinstance(batch, list):
                out.extend([m for m in batch if isinstance(m, dict)])
            nxt = str(data.get("next_cursor") or "")
            if not nxt or nxt == cursor or not batch:
                break
            cursor = nxt
        return out

    # ---------- 主动发群消息 ----------
    async def send_group_message(self, group_openid: str, content: str,
                                 event_id: str = "", msg_id: str = "",
                                 markdown: bool = False) -> dict:
        """向群内发送文本 / Markdown 消息。

        传 event_id / msg_id 时走**被动消息**（官方推荐）：
        官方明确支持用 GROUP_ADD_ROBOT 等事件的 id 作为 event_id 做被动回复，
        5 分钟内有效、同一个事件最多回 5 条，且不吃主动消息频控。
        不传则是主动消息：受主动消息频控（未认证 30 条/分钟、单群 20 条/分钟、
        每群每天 1000 条），并且用户可在客户端关闭「允许主动发送」。

        Args:
            markdown: True 时按 msg_type=2 发自定义 Markdown（``# 标题``、
                ``**加粗**``、``[🔗文字](url)`` 等），2026/04/23 起单聊/群聊
                都无需单独申请 Markdown 模板。默认 False＝纯文本 msg_type=0。
        """
        self._seq = (self._seq + 1) % 100000
        msg_seq = self._seq or 1
        body = {
            "msg_type": 2 if markdown else 0,
            "msg_seq": msg_seq,
        }
        if markdown:
            body["markdown"] = {"content": content}
        else:
            body["content"] = content
        if event_id:
            body["event_id"] = event_id
        elif msg_id:
            body["msg_id"] = msg_id
        return await self._request(
            "POST",
            f"/v2/groups/{group_openid}/messages",
            body=body,
        )

    # ---------- 撤回群消息 ----------
    async def recall_group_message(self, group_openid: str, message_id: str) -> dict:
        """撤回群消息（成功返回 HTTP 200/204，无响应体）。

        官方限制:
        - 发送超过 2 分钟的消息不可撤回（错误码 40064004）。
        - 机器人是群管理员时，可撤回自己的消息以及普通群成员的消息；
          机器人是普通成员时，只能撤回自己发送的消息（错误码 40062003）。
        文档: DELETE /v2/groups/{group_openid}/messages/{message_id}
        """
        return await self._request(
            "DELETE",
            f"/v2/groups/{group_openid}/messages/{message_id}",
        )

    # ---------- 工具 ----------
    @staticmethod
    def format_rfc3339(ts: float) -> str:
        """unix 时间戳 -> RFC3339（+08:00 时区）。"""
        import datetime as _dt

        dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone(_dt.timedelta(hours=8)))
        return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    # ---------- 入群自动审批策略 ----------
    # 官方文档：/v2/groups/join_approval_strategy
    # 注意：策略仅在机器人拥有群管理员身份时才会实际生效。
    async def list_join_approval_strategies(self, cursor: str = "",
                                            limit: int = 20) -> dict:
        """查询生效中的策略列表（按创建时间倒序）。

        返回 {'strategies': [...], 'next_cursor': str}，next_cursor 为空串表示末页。
        """
        params: dict = {"limit": max(1, min(int(limit or 20), 50))}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", "/v2/groups/join_approval_strategy", params=params
        )

    async def create_join_approval_strategy(self, group_openids: list[str] | None = None,
                                            group_ids: list[int] | None = None,
                                            is_enable: str = "on",
                                            expire_at: str | None = None,
                                            remark: str | None = None) -> dict:
        """创建策略。group_openids 与 group_ids 二选一必填，各自最多 100 个。

        expire_at 为 RFC3339；不传则默认一年后过期。strategy_id 由服务端生成。
        """
        if bool(group_openids) == bool(group_ids):
            raise QQApiError("group_openids 与 group_ids 必须二选一传入")
        body: dict = {"is_enable": is_enable if is_enable in ("on", "off") else "on"}
        if group_openids:
            body["group_openids"] = list(group_openids)[:100]
        else:
            body["group_ids"] = list(group_ids)[:100]
        if expire_at:
            body["expire_at"] = expire_at
        if remark:
            body["remark"] = remark[:255]
        return await self._request(
            "POST", "/v2/groups/join_approval_strategy", body=body
        )

    async def update_join_approval_strategy(self, strategy_id: str,
                                            is_enable: str | None = None,
                                            expire_at: str | None = None,
                                            group_action: dict | None = None,
                                            remark: str | None = None) -> dict:
        """修改策略的启停 / 过期时间 / 备注 / 关联群增删。

        group_action 形如 {'op': 'add'|'del', 'group_openids': [...]} 或
        {'op': 'del', 'group_ids': [...]}；群标识形式须与创建时一致。
        """
        if not strategy_id:
            raise QQApiError("strategy_id 不能为空")
        body: dict = {}
        if is_enable in ("on", "off"):
            body["is_enable"] = is_enable
        if expire_at:
            body["expire_at"] = expire_at
        if group_action:
            if group_action.get("op") not in ("add", "del"):
                raise QQApiError("group_action.op 只能是 add / del")
            body["group_action"] = group_action
        if remark is not None:
            body["remark"] = str(remark)[:255]
        if not body:
            raise QQApiError("没有需要修改的字段")
        return await self._request(
            "PATCH", f"/v2/groups/join_approval_strategy/{strategy_id}", body=body
        )

    async def delete_join_approval_strategy(self, strategy_id: str) -> dict:
        """删除策略，成功返回空 dict。"""
        if not strategy_id:
            raise QQApiError("strategy_id 不能为空")
        return await self._request(
            "DELETE", f"/v2/groups/join_approval_strategy/{strategy_id}"
        )

    async def execute_join_approval_strategy(self, strategy_id: str) -> dict:
        """立即全量扫描策略关联的所有群，命中白名单的入群申请自动通过。

        异步执行，官方说明约 10 分钟完成；接口返回即代表任务已受理。
        """
        if not strategy_id:
            raise QQApiError("strategy_id 不能为空")
        return await self._request(
            "POST", f"/v2/groups/join_approval_strategy/{strategy_id}/execute"
        )

    async def update_join_approval_whitelist(self, strategy_id: str, op: str,
                                             users: list[str]) -> dict:
        """批量增删白名单 QQ 号码。单次最多 10000 个，策略上限 10W。

        号码一律按字符串传，避免大整数精度丢失。
        """
        if not strategy_id:
            raise QQApiError("strategy_id 不能为空")
        if op not in ("add", "del"):
            raise QQApiError("op 只能是 add / del")
        cleaned = [str(u).strip() for u in (users or []) if str(u).strip()]
        if not cleaned:
            raise QQApiError("白名单号码不能为空")
        if len(cleaned) > 10000:
            raise QQApiError(f"单次最多 10000 个号码，当前 {len(cleaned)} 个")
        return await self._request(
            "POST",
            f"/v2/groups/join_approval_strategy/{strategy_id}/whitelist_users",
            body={"op": op, "whitelist_users": cleaned},
        )
