"""QQ 官方内联键盘（按钮）与回调数据的组装 / 解析。

为什么单独成一个模块
--------------------
按钮涉及三层数据，缺一层就「发得出、回不来」：

1. **渲染层**：``render_data.label``（最多 10 字）、``style``、``visited_label``；
2. **动作层**：``action.type``（0 跳转 / 1 回调 / 2 指令）、``permission``、``data``；
3. **回传层**：用户点击后平台把 ``data`` 原样带回来（``INTERACTION_CREATE``
   事件里的 ``data.resolved.button_data``）。

业务代码里直接拼这三层很容易漏掉回传层，所以这里提供「发」与「收」配对的两个
函数：``encode_join_action()`` 产出 data，``parse_join_action()`` 解析回来。

安全前提（重要）
----------------
``button_data`` 是**不可信输入**：客户端可以把任意字符串塞回来，甚至伪造出
我们从未发过的按钮。因此这里解析出的内容一律只当作「意图提示」，
真实数据必须以服务端自己的待审批列表为准（见 ``main._on_button_interaction``：
取到 member_openid + apply_at 后，去 ``_pending`` 里比对，比对不上直接拒绝）。

文档
----
- 互动事件: https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html
- 互动事件响应: https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/interactions_interaction_id.put.html
- 发送群聊消息（keyboard 字段）: https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html
"""

import base64
import hashlib
import json
import re
from typing import Any, Iterable, Optional

# 官方未公开内联键盘的行列硬上限，超限返回 40034029（内联键盘行/列超限）。
# 这里按社区实测的 5×5 保守封顶，宁可少放几个按钮，也不要整条消息发不出去。
MAX_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
LABEL_MAX = 10          # render_data.label 官方上限
DATA_MAX = 512          # action.data 保守上限（官方未给硬限，超长容易被截断）
# group_id（按钮互斥分组）官方文档只写了「分组ID」，没给格式约束，但实测：
# markdown 消息（msg_type=2）下带 `ja:<openid 尾 8 位>:<RFC3339 时间>` 这种
# 含冒号、加号、长度 36 的值会被判 40011021「请求参数rows.buttons.group_id无效」，
# 整条消息拒发；同样内容用文本消息（msg_type=0）却能过。
# 结论：这个字段在 markdown 通道里校验更严，只能喂「短、纯 ASCII、无标点」的值。
GROUP_ID_MAX = 32       # 保守上限，短于任何可能存在的硬限
_GROUP_ID_BAD = re.compile(r"[^0-9A-Za-z_-]")

# 回调数据的命名空间前缀，避免与其他插件/其他用途的按钮串味。
CALLBACK_PREFIX = "qgm:ja:1:"

# 动作语义
OP_APPROVE = "a"
OP_DECLINE = "d"

# 对应「互动事件响应」PUT 请求体的 code 取值，供日志/文档引用
INTERACTION_CODES = {
    0: "成功",
    1: "操作失败",
    2: "操作频繁",
    3: "重复操作",
    4: "没有权限",
    5: "仅管理员操作",
}


def _b64e(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(text: str) -> str:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad).decode("utf-8")


# ----------------------------------------------------------------------
# 回传层：审批按钮的 data 编解码
# ----------------------------------------------------------------------
def encode_join_action(op: str, member_openid: str, apply_at: str) -> str:
    """把「对某个入群申请的动作」编码成按钮 data。

    用 ``(member_openid, apply_at)`` 而不是 ``join_request_id``，原因与
    ``main._req_key`` 一致：官方 ``join_request_id`` 每次查询都会变，
    拿它当标识会导致回调时对不上号。``apply_at`` 是申请时间，稳定且唯一。

    Args:
        op: ``OP_APPROVE`` / ``OP_DECLINE``。
        member_openid: 申请人 openid。
        apply_at: 申请时间（RFC3339 原样）。

    Returns:
        形如 ``qgm:ja:1:<base64url>`` 的按钮 data 字符串。
    """
    payload = {"o": str(op or "")[:1], "m": str(member_openid or ""), "t": str(apply_at or "")}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (CALLBACK_PREFIX + _b64e(raw))[:DATA_MAX]


def parse_join_action(data: str) -> Optional[dict]:
    """解析按钮 data，返回 ``{'op': 'a'|'d', 'member_openid': str, 'apply_at': str}``。

    任何异常（不是我们的 data、base64 坏了、字段缺失、op 不认识）一律返回 None，
    绝不抛异常——回调路径上抛异常会让客户端一直转圈。
    """
    text = str(data or "").strip()
    if not text.startswith(CALLBACK_PREFIX):
        return None
    try:
        payload = json.loads(_b64d(text[len(CALLBACK_PREFIX):]))
    except Exception:  # noqa: BLE001 - 不可信输入，任何解析失败都按「不是我们的」处理
        return None
    if not isinstance(payload, dict):
        return None
    op = str(payload.get("o") or "")
    member = str(payload.get("m") or "").strip()
    apply_at = str(payload.get("t") or "").strip()
    if op not in (OP_APPROVE, OP_DECLINE) or not member or not apply_at:
        return None
    return {"op": op, "member_openid": member, "apply_at": apply_at}


# ----------------------------------------------------------------------
# 动作层 / 渲染层：按钮与键盘结构
# ----------------------------------------------------------------------
def make_group_id(*parts: Any) -> str:
    """把任意业务标识压成一个「按钮互斥分组 ID」。

    为什么要哈希而不是直接拼：官方没公开这个字段的格式约束，实测含冒号/加号的
    长字符串在 markdown 消息里会被判 40011021（见 GROUP_ID_MAX 注释）。哈希后
    固定 17 位、只含小写十六进制 + 前缀 ``g``，任何校验都能过；同输入必定同输出，
    所以「同一申请的两个按钮」仍然共用一个分组、点一个另一个变灰。
    """
    raw = "|".join(str(p or "") for p in parts).encode("utf-8")
    return "g" + hashlib.md5(raw).hexdigest()[:16]  # noqa: S324 - 非安全用途，只要稳定短 ID


def safe_group_id(value: str) -> str:
    """把外部传入的 group_id 清洗成短 ASCII；清洗后为空则返回空串（= 不带该字段）。"""
    return _GROUP_ID_BAD.sub("", str(value or ""))[:GROUP_ID_MAX]


def has_group_id(keyboard: Optional[dict]) -> bool:
    """键盘里是否有按钮带了 group_id（带了才需要准备「去掉它」的降级版本）。"""
    for row in ((keyboard or {}).get("content") or {}).get("rows") or []:
        for btn in row.get("buttons") or []:
            if isinstance(btn, dict) and btn.get("group_id"):
                return True
    return False


def without_group_id(keyboard: Optional[dict]) -> Optional[dict]:
    """返回一份「没有 group_id」的键盘副本（保留 data 路由，只丢互斥变灰效果）。

    markdown 通道对 group_id 校验更严，被拒时优先丢它而不是丢整排按钮。
    """
    if not keyboard:
        return None
    try:
        clone = json.loads(json.dumps(keyboard))
    except Exception:  # noqa: BLE001
        return None
    for row in ((clone.get("content") or {}).get("rows") or []):
        for btn in row.get("buttons") or []:
            if isinstance(btn, dict):
                btn.pop("group_id", None)
    return clone


def make_button(
    button_id: str,
    label: str,
    data: str,
    *,
    style: int = 0,
    visited_label: str = "",
    group_id: str = "",
    permission: str = "admin",
    action_type: int = 1,
    unsupport_tips: str = "",
) -> dict:
    """组装单个按钮。

    Args:
        button_id: 同一键盘内唯一，同时作为 AstrBot 侧的 action_id。
        label: 按钮文字（超过 10 字会截断）。
        data: ``action.type=1`` 时的回调数据（``encode_join_action`` 的产物）。
        style: 0 灰色线框 / 1 蓝色线框 / 3 白底红字 / 4 蓝底白字。
        visited_label: 点击后显示的文案，不传则保持不变。
        group_id: 同组按钮点一个后其余自动变灰——用它在「通过/拒绝」之间互斥。
        permission: ``admin``（仅群主/管理员可点，官方 permission.type=1）
            或 ``everyone``（所有人可点，permission.type=2，此时必须由插件
            自己复核操作者权限）。
        action_type: 0 跳转 / 1 回调 / 2 指令。审批用 1。
        unsupport_tips: 客户端版本过低时的提示文案。
    """
    render: dict = {"label": str(label or "")[:LABEL_MAX], "style": int(style)}
    if visited_label:
        render["visited_label"] = str(visited_label)[:LABEL_MAX]
    btn: dict = {
        "id": str(button_id or "")[:64],
        "render_data": render,
        "action": {
            "type": int(action_type),
            "data": str(data or "")[:DATA_MAX],
            "permission": {"type": 1 if permission == "admin" else 2},
        },
    }
    if group_id:
        gid = safe_group_id(group_id)
        if gid:
            btn["group_id"] = gid
    if unsupport_tips:
        btn["action"]["unsupport_tips"] = str(unsupport_tips)[:32]
    return btn


def make_keyboard(rows: Iterable[Iterable[dict]]) -> Optional[dict]:
    """把按钮行组装成官方 ``keyboard`` 结构，超限自动截断。"""
    clean_rows: list[dict] = []
    for row in rows or []:
        buttons = [b for b in (row or []) if isinstance(b, dict)][:MAX_BUTTONS_PER_ROW]
        if buttons:
            clean_rows.append({"buttons": buttons})
        if len(clean_rows) >= MAX_ROWS:
            break
    if not clean_rows:
        return None
    return {"content": {"rows": clean_rows}}


def normalize_keyboard(spec: Any) -> Optional[dict]:
    """把「面板里配的键盘」规范化成官方结构。

    支持三种写法：
    1. 已经是官方结构 ``{"content": {"rows": [...]}}`` → 原样（但会过一遍截断）；
    2. 只有 rows 列表 ``[{"buttons": [...]}, ...]`` → 包一层；
    3. 自定义按钮 JSON 字符串 → 先 json 解析再按上面两条处理。

    解析不出来一律返回 None（= 这条消息不带键盘），绝不因为按钮配错就把
    整条消息发不出去。
    """
    if not spec:
        return None
    data = spec
    if isinstance(spec, str):
        text = spec.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(data, dict):
        if isinstance(data.get("content"), dict):
            data = data["content"].get("rows")
        elif isinstance(data.get("rows"), list):
            data = data["rows"]
        else:
            return None
    if not isinstance(data, list):
        return None
    rows = []
    for row in data:
        if isinstance(row, dict):
            rows.append(row.get("buttons") or [])
        elif isinstance(row, list):
            rows.append(row)
    return make_keyboard(rows)


def join_request_keyboard(
    requests: list[dict],
    *,
    permission: str = "admin",
    max_requests: int = 3,
) -> Optional[dict]:
    """为若干个入群申请生成「通过 / 拒绝」按钮键盘。

    每个申请一行两键，同一 ``group_id`` → 点其中一个后同行的另一个变灰，
    防止管理员「通过」完又点「拒绝」。请求数超过行数上限时自动截断
    （剩下的走 ``/群管 审批`` 指令处理）。

    Args:
        requests: 申请条目列表（需含 ``member_openid`` / ``apply_at`` / ``username``）。
        permission: 见 ``make_button``。
        max_requests: 最多放几个申请，受 ``MAX_ROWS`` 限制。
    """
    rows: list[list[dict]] = []
    limit = max(1, min(int(max_requests or 1), MAX_ROWS))
    for pos, req in enumerate(list(requests or [])[:limit]):
        member = str(req.get("member_openid") or "")
        apply_at = str(req.get("apply_at") or "")
        if not member or not apply_at:
            continue
        name = str(req.get("username") or member[-6:] or "申请人")
        # 同一申请的两个按钮共用一个分组 → 点一个另一个变灰。
        # 值必须是短 ASCII（见 make_group_id 注释），不能用原始 openid + 时间戳。
        gid = make_group_id(member, apply_at)
        # 按钮 id 在同一消息内必须唯一（官方限制），所以带上行序号；
        # 业务路由不靠 id，靠的是 data（见模块头「安全前提」）。
        rows.append([
            make_button(
                f"qgm_a{pos}",
                "通过",
                encode_join_action(OP_APPROVE, member, apply_at),
                style=1,
                visited_label="已通过",
                group_id=gid,
                permission=permission,
                unsupport_tips=f"低版本请回复 /群管 通过 {name}",
            ),
            make_button(
                f"qgm_d{pos}",
                "拒绝",
                encode_join_action(OP_DECLINE, member, apply_at),
                style=3,
                visited_label="已拒绝",
                group_id=gid,
                permission=permission,
                unsupport_tips=f"低版本请回复 /群管 拒绝 {name}",
            ),
        ])
    return make_keyboard(rows)


def describe_keyboard(kb: Optional[dict]) -> str:
    """给日志用的键盘摘要：``3行/5键`` 这种，避免把整段 JSON 打进日志。"""
    rows = ((kb or {}).get("content") or {}).get("rows") or []
    total = sum(len(r.get("buttons") or []) for r in rows)
    return f"{len(rows)}行/{total}键"
