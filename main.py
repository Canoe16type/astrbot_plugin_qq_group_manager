"""QQ 官方机器人群管理插件。

功能:
- 指令禁言/解禁/查禁言（POST /v2/groups/{group_openid}/restrict_chat_setting）
- 入群申请轮询通知 + 指令审批（/群管 审批、/群管 通过、/群管 拒绝）
- 入群欢迎消息（审批通过后 + 机器人进入新群首次交互）
- 多模式管理员判定（AstrBot 管理员 / QQ 群角色 / 自定义 OpenID，WebUI 可配）
- 机器人身份（AppID/Secret）从当前平台动态获取，不写死
- 群成员退群/被移出通知（运行时注入，见 member_event.py）
- WebUI：概览 / 群管 / 群规审核 / 记录 / 日志（SSE 实时日志）

说明:
- 不修改 AstrBot 本体任何代码。
- 入群欢迎通过"审批通过"与"新群首次交互"两种方式实现；
  退群通知依赖对运行中的 qq_official 适配器做一次性事件注入。

生成声明:
- 本插件由 AI 生成，代码经人工审核与测试后发布。
"""

import asyncio
import ipaddress
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import At, Image, Plain
# 只使用 astrbot.api 公开接口：不 import astrbot.core.* 内部实现。
# 面板「管理行为 → 指令」的禁用由 AstrBot 自己在 get_handlers_by_event_type()
# 里按 handler.enabled 过滤（见 star_handler.py），被禁用的指令根本不会回调到插件，
# 插件无需也无法插手 —— 早先为此去读 star_handlers_registry 属于私有 API 依赖，已移除。
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import json_response, request, stream_response

from .plugin_logger import PluginLogger
from .qq_api import QQApi, QQApiError
from . import button_kit
from . import member_event
from . import member_registry as member_registry_lib
from . import lexicon as lexicon_lib
from . import moderation
from . import variant as variant_lib

# ----------------------------------------------------------------------
# 按钮点击通道能力探测
# ----------------------------------------------------------------------
# AstrBot 4.29 起，按钮点击事件在「唤醒检查」阶段**只会交给挂了
# ButtonActionFilter 的 handler**，其余 handler 一律被 continue 掉
# （核心实现见 astrbot/core/pipeline/waking_check/stage.py：
#   if event.is_button_interaction() and not any(
#       isinstance(f, ButtonActionFilter) for f in handler.event_filters
#   ): continue
# ）
# 推论：普通的消息 handler（event_message_type）永远收不到按钮点击，
# 必须用一个独立的 @filter.button_action() handler 来接。
# 旧版本 AstrBot 没有这个 filter，那就干脆不渲染按钮，
# 免得出现「点得动但服务端毫无反应」的死按钮。
_BUTTON_ACTION_DECORATOR = getattr(filter, "button_action", None)
_BUTTON_ACTION_SUPPORTED = callable(_BUTTON_ACTION_DECORATOR)


class _TplText(str):
    """带模板 key 的字符串。

    _render() 返回它，_reply() 据此查出该模板配置的发送格式（text / markdown / image）。
    调用方如果对返回值做过拼接、切片等操作，会退化成普通 str，此时按纯文本发送 ——
    这是安全降级，不改变任何既有调用点的行为。
    """

    tpl_key: str = ""
    # 该模板对应的「成员」openid：{avatar} 头像模块用它取图。
    # {at} 在 _render() 内已展开成标签，这里只留给头像用。
    tpl_target: str = ""

    def __new__(
        cls, value: str = "", key: str = "", target: str = ""
    ) -> "_TplText":
        obj = str.__new__(cls, value)
        obj.tpl_key = key
        obj.tpl_target = target
        return obj

# 唯一的插件名来源＝插件目录名（也就是 AstrBot 的插件 id）。
# AstrBot 对这四样东西都按同一个 id 定位，任何一处写死别的名字都会出问题：
#   1) data/plugins/<id>/                       插件本体
#   2) data/config/<id>_config.json             插件配置（卸载时按此名删除）
#   3) data/plugin_data/<id>/                   插件持久化数据（卸载时按此名删除）
#   4) /api/v1/plugins/extensions/<id>/<接口>   面板 Web API 路由前缀
# 1.0.0 及更早版本曾把数据写进 plugin_data/qq_group_manager，导致卸载时
# AstrBot 找不到该目录、静默跳过删除（"删了数据其实没删"）。此处统一为目录名，
# 并通过 _migrate_legacy_data_dir() 一次性迁移旧目录。
PLUGIN_NAME = os.path.basename(os.path.dirname(os.path.abspath(__file__))) or "astrbot_plugin_qq_group_manager"
# 日志出口统一走 astrbot.api.logger（AstrBot 会按插件路由到 astrbot.plugin.<id>，
# 收进 data/logs）。初始化早期（self.logger / PluginLogger 还没构造完）也用它，
# 插件不得使用 stdlib logging，出口只能是 astrbot.api.logger。
WEB_API_PREFIX = PLUGIN_NAME
# 腾讯 QQ 官方机器人的两种接入形态（都是同一套开放平台 API、同样的 appid/secret）：
#   - qq_official：AstrBot 内置 WebSocket 适配器（botpy），成员事件注入只对它有效；
#   - qq_official_webhook：4.16 起提供的 Webhook 适配器，事件由平台推到本地 HTTP。
# 面板/API 只关心「有哪些机器人实例、appid 是什么」，所以两种都要认；
# 否则用 Webhook 模式的人打开面板会看到「没有机器人」，所有页签都是空的。
QQ_PLATFORM_NAMES = ("qq_official", "qq_official_webhook")
# 旧版数据目录名，仅用于迁移与清理兼容，禁止再用于新路径
LEGACY_PLUGIN_NAME = "qq_group_manager"

# ---- 一键获取上游词库 ----
LEXICON_UPSTREAM_NAME = "上游词库（一键获取）"
# 刻意不内置任何下载地址：词表由使用者自行寻找/自行提供，插件只负责「下载 + 解析 + 落地」。
LEXICON_UPSTREAM_URL = ""
LEXICON_UPSTREAM_MAX_BYTES = 2 * 1024 * 1024      # 单次下载上限 2 MB
LEXICON_UPSTREAM_TIMEOUT = 15.0                   # 下载超时（秒）
LOG_SSE_HEARTBEAT_SEC = 15                         # 日志 SSE 空闲保活间隔（秒）；不发心跳会被反代按空闲超时掐断
LEXICON_UPSTREAM_FAST_TIMEOUT = 8.0               # 配了加速地址时，直连只等这么久


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向时复检目标地址，防止 302 跳内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        err = QQGroupManager._check_url_public(newurl)
        if err:
            raise ValueError(f"重定向到不安全的地址（{err}）")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_plugin_version() -> str:
    """从 metadata.yaml 读插件版本（面板脚注用，避免手写版本号忘了同步）。"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.yaml")
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return ""


PLUGIN_VERSION = _read_plugin_version()


def _write_json_atomic(path: str, payload, *, indent: int = 1) -> None:
    """原子写 JSON：先写临时文件，再 os.replace 顶上去。

    这些文件（群列表 / 禁言历史 / 撤回历史 / 自我介绍状态）都是「整份重写」，
    直接 open(w) 会在写一半时崩溃、掉电、磁盘写满的情况下留下被截断的 JSON，
    下次启动 json.load 失败 → 整份数据静默清零。临时文件 + os.replace 是
    同目录下的原子替换，最坏情况是保留旧文件，不会丢结构。
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".avif")


def _looks_like_image_url(u: str) -> bool:
    """按扩展名判断是不是图片地址（去 query，官方 URL 后面常带签名参数）。"""
    path = str(u or "").split("?", 1)[0].lower()
    return path.endswith(_IMAGE_EXTS)


def _norm_media_url(u: str) -> str:
    """官方 attachment 的 url 有时是 // 开头或裸域名，补成 https。"""
    u = str(u or "").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("//"):
        return "https:" + u
    return "https://" + u


def _patch_botpy_quote_fields() -> None:
    """Monkey-patch botpy 消息类，补齐 QQ 官方群消息事件里的引用字段。

    背景：QQ 官方群消息事件中，引用消息（message_type=103）的被引用消息
    ID 位于 msg_elements[0].msg_idx（REFIDX 格式），原文位于
    msg_elements[0].content，引用索引在 message_scene.ext（ref_msg_idx）。
    但 botpy 的 BaseMessage/GroupMessage 的 __slots__ 不含 message_type /
    msg_elements / message_scene，构造时这些字段被直接丢弃，导致 AstrBot
    qq_official 适配器无法识别引用消息、永远不生成 Reply 组件，插件也就
    拿不到"被引用了哪条消息"。

    类已定义后无法真正扩展 __slots__，因此采用"原始 payload 外置存储 +
    __getattr__ 兜底"的方式：构造消息时把完整 payload 存进 WeakKeyDictionary，
    当适配器访问缺失字段（message_type / msg_elements / message_scene）时
    由 __getattr__ 从 payload 中返回，使 AstrBot 适配器现有的引用解析逻辑
    （message_type==103 + msg_elements[0]）得以生效。
    不修改 AstrBot 本体代码；patch 失败不影响插件其余功能。
    """
    try:
        import botpy.message as _botpy_message

        base = _botpy_message.BaseMessage
        if getattr(base, "_qq_group_manager_quote_patched", False):
            return

        # 注意：botpy 消息类用 __slots__ 且未含 __weakref__，实例既不能
        # weakref 也无法挂载新属性，因此用 id() 索引的普通 dict 外置存储，
        # 带 FIFO 上限防止内存膨胀；读取时校验 self.id 防止 id 复用误读。
        _extra_by_id: dict[int, dict] = {}
        _MAX_EXTRA = 2000
        base._qq_group_manager_quote_data = _extra_by_id

        _orig_init = base.__init__

        def _patched_init(self, api, event_id, data):
            _orig_init(self, api, event_id, data)
            try:
                if isinstance(data, dict):
                    _extra_by_id[id(self)] = data
                    if len(_extra_by_id) > _MAX_EXTRA:
                        for _k in list(_extra_by_id)[: len(_extra_by_id) - _MAX_EXTRA]:
                            del _extra_by_id[_k]
            except Exception:
                pass

        base.__init__ = _patched_init

        # slots 类实例访问缺失属性时会触发 __getattr__，用它兜底读取
        def _patched_getattr(self, name):
            if name in ("message_type", "msg_elements", "message_scene"):
                data = _extra_by_id.get(id(self))
                if isinstance(data, dict) and data.get("id") == getattr(self, "id", None):
                    return data.get(name)
                return None
            raise AttributeError(name)

        base.__getattr__ = _patched_getattr
        base._qq_group_manager_quote_patched = True
    except Exception as e:  # pragma: no cover
        logger.warning(
            f"botpy 引用字段 patch 失败（引用撤回可能不可用）: {e}"
        )


_patch_botpy_quote_fields()

DURATION_RE = re.compile(r"^\s*(\d+)\s*(秒|分钟|分|小时|时|天|s|m|h|d)?\s*$", re.I)

# 管理指令集合（需要管理员权限）
# 管理员指令：新的「大类」+ 旧的独立写法都列在这里（旧写法会先被 _normalize_aliases 折进大类）
ADMIN_COMMANDS = ("/禁言", "/审批", "/状态", "/撤回", "/审核", "/管理登记", "/成员", "/撤销",
                  "/解禁", "/查禁言", "/禁言历史", "/通过", "/拒绝", "/群名", "/汇报", "/体检", "/自检")

# 本插件注册到 AstrBot「管理行为 → 指令」面板的规范指令名（不带 /）。
# 说明：这些名字现在挂在指令组「群管」下作为子指令（面板里是 1 条组 + 子指令），
# 能搜索、能单独禁用、能改别名/改权限；真正的权限判定（AstrBot 管理员 /
# QQ 群角色 / 自定义 OpenID）仍由插件内 _is_admin 负责，比 AstrBot 的
# "仅管理员"更宽松。
# 1.5.0 起对外只推「大类」：禁言 / 审批 / 状态 / 审核 / 群规 / 撤回 / 举报 / 成员 / 管理登记。
# 旧的独立指令（解禁、查禁言、禁言历史、通过、拒绝、汇报、体检、自检、群名）
# 仍作为别名挂在对应大类下，老写法继续可用。
REGISTERED_COMMANDS = (
    "禁言", "审批", "状态", "撤回", "审核", "群规", "撤销",
    "管理登记", "群管", "举报", "成员", "帮助",
)

# 禁言历史：每群最多保留的条数
HISTORY_MAX_PER_GROUP = 500


# 退群清理：各类数据的可读名（日志 / 回执里给用户看）
_PURGE_LABELS = {
    "known_groups": "已知群记录",
    "mute_history": "禁言历史",
    "recall_history": "撤回历史",
    "members": "成员档案",
    "rules": "群规",
    "settings": "审核设置",
    "verdicts": "判定记录",
    "group_meta": "群名/备注",
    "admins": "提醒名单",
    "templates": "群话术覆盖",
    "welcome_dedupe": "入群欢迎去重",
    "notify_task": "待推送通知",
    "nick_cache": "昵称缓存",
    "pending": "申请缓存",
}


def _purge_summary_text(summary: dict | None, empty: str = "无残留数据") -> str:
    """把清理结果字典拼成一句人能读的话。"""
    if not summary:
        return empty
    parts = []
    for k, v in summary.items():
        label = _PURGE_LABELS.get(k, k)
        try:
            n = int(v or 0)
        except (TypeError, ValueError):
            n = 0
        parts.append(f"{label} {n} 条" if n > 1 else label)
    return "、".join(parts)


def _recall_hint(err: object) -> str:
    """把 QQ 官方撤回接口的错误码翻译成人话（官方文档「撤回群聊消息」）。"""
    s = str(err)
    if "40064004" in s or "撤回时限" in s:
        return "官方 2 分钟撤回时限已过（错误码 40064004）"
    if "40062003" in s or "无操作权限" in s:
        return ("机器人无撤回权限（错误码 40062003）：需把机器人设为群管理员，"
                "且被撤回者需为普通群成员（群主/管理员的消息撤不了）")
    if "50065001" in s:
        return "平台侧撤回失败，可稍后重试（错误码 50065001）"
    return s or "撤回失败"


def fmt_ts(ts: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """unix 时间戳 -> 本地可读时间。"""
    try:
        return datetime.fromtimestamp(ts).strftime(fmt)
    except Exception:
        return str(ts)


# 中文数字映射（"一分钟" -> 1）
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_duration(text: str) -> Optional[int]:
    """解析时长文本为秒数。支持 30秒/5分钟/2小时/1天/600(秒)/一分钟。"""
    m = DURATION_RE.match(text or "")
    if not m:
        # 兼容中文数字写法："一分钟"→60s、"半小时"→1800s（十以上暂不展开）
        t = (text or "").strip()
        if len(t) == 3 and t[0] in _CN_NUM and t[1:] in ("秒", "分", "分钟", "时", "小时", "天"):
            return parse_duration(f"{_CN_NUM[t[0]]}{t[1:]}")
        if len(t) == 3 and t[0] == "半":
            if t[1:] in ("分", "分钟"):
                return 30
            if t[1:] in ("时", "小时"):
                return 30 * 60
        return None
    num = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit in ("s", "秒"):
        return num
    if unit in ("m", "分钟", "分"):
        return num * 60
    if unit in ("h", "小时", "时"):
        return num * 3600
    if unit in ("d", "天"):
        return num * 86400
    return num


# 群规「一行式」写法里的分隔符：全角竖线 / 半角竖线 / 兼容 U+FE31 竖排形式。
# 教训：以前只认全角「｜」，用户用半角「|」时整句会被当成规则名，
# 说明和时长静默丢失（群里表现为「已添加群规：侮辱」）。
_RULE_SEP_RE = re.compile(r"\s*[｜|︱]\s*")


def parse_rule_spec(text: str) -> tuple[str, str, int, Optional[str]]:
    """解析群规的一行式写法，返回 (name, desc, seconds, error)。

    error 非 None 时调用方应回用法提示，而不是硬建一条残缺群规。

    三种写法都认（都是用户真实会发出来的）：
      名｜说明｜时长（全角竖线，帮助里的标准写法）
      名 | 说明 | 时长（半角竖线，手机输入法/英文键盘最常见）
      名 说明 时长（纯空格）
    时长可省略 → 默认 1 小时；只写一段 → 只当规则名。
    """
    raw = (text or "").strip()
    if not raw:
        return "", "", 3600, "empty"
    norm = _RULE_SEP_RE.sub("｜", raw)
    if "｜" in norm:
        seg = [x.strip() for x in norm.split("｜") if x.strip()]
    else:
        seg = [x.strip() for x in norm.split() if x.strip()]
    seconds = 3600
    if len(seg) >= 2:
        tail = parse_duration(seg[-1])
        if tail:
            seconds = int(tail)
            seg = seg[:-1]
    if not seg:
        return "", "", 3600, "empty"
    name = seg[0]
    desc = " ".join(seg[1:]).strip()
    if len(name) > 24:
        return name, desc, seconds, "name_too_long"
    return name, desc, seconds, None


RULE_USAGE_ADD = (
    "用法：/群管 群规 添加 规则名 | 说明 | 时长" + chr(10) +
    "示例：/群管 群规 添加 刷屏 | 连续刷无意义内容 | 10分钟" + chr(10) +
    "竖线用半角「|」或全角「｜」都行；时长可省略，默认 1 小时。"
)
RULE_USAGE_SET = (
    "用法：/群管 群规 设置 规则名 | 说明 | 时长" + chr(10) +
    "示例：/群管 群规 设置 刷屏 | 连续刷无意义内容 | 10分钟" + chr(10) +
    "多条用「;」隔开，规则会追加到本群群规里。"
)


def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分钟"
    if seconds < 86400:
        return f"{seconds // 3600}小时"
    return f"{seconds // 86400}天"


# 面板与消息里的时间统一按 AstrBot 全局配置 timezone 渲染（默认 Asia/Shanghai / UTC+8），
# 不跟随宿主机或浏览器时区；时长类配置一律以「秒」为单位，只在展示时换算。
_ACTIVE_TZ_NAME = "Asia/Shanghai"


def active_timezone_name() -> str:
    return _ACTIVE_TZ_NAME


def _active_tz():
    try:
        return ZoneInfo(_ACTIVE_TZ_NAME)
    except Exception:
        return None


def set_active_timezone(name: str) -> str:
    """写入生效时区；非法时区名回退 Asia/Shanghai。"""
    global _ACTIVE_TZ_NAME
    name = str(name or "").strip() or "Asia/Shanghai"
    try:
        ZoneInfo(name)
    except Exception:
        name = "Asia/Shanghai"
    _ACTIVE_TZ_NAME = name
    return name


def fmt_rfc3339(rfc: str) -> str:
    """RFC3339 -> 按 AstrBot 配置时区渲染的可读时间。"""
    try:
        # 兼容 Z / +08:00 结尾
        s = rfc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        tz = _active_tz()
        dt = dt.astimezone(tz) if tz is not None else dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return rfc


def _mute_rule_live(mode: str, windows: list) -> bool:
    """判断「此刻」是否真的落在某条已启用禁言时段里（按 AstrBot 配置时区）。

    只负责算，不负责渲染：面板据此把「已启用」和「此刻正在禁言」分开显示，
    免得一条已过期/还没到点的规则被当成正在禁言。算不出来时返回 False（宁少报不多报）。
    """
    if mode == "always":
        return True
    if mode != "schedule":
        return False
    try:
        now = datetime.now(_active_tz()) if _active_tz() is not None else datetime.now().astimezone()
    except Exception:
        return False
    for w in windows:
        if not w.get("enabled"):
            continue
        try:
            if w.get("kind") == "recurring":
                days = [d for d in (w.get("weekdays") or []) if 1 <= d <= 7]
                if not days:
                    continue
                sh, sm = (str(w.get("start") or "").split(":") + ["0", "0"])[:2]
                eh, em = (str(w.get("end") or "").split(":") + ["0", "0"])[:2]
                st = int(sh) * 60 + int(sm)
                en = int(eh) * 60 + int(em)
                cur = now.hour * 60 + now.minute
                wd = now.isoweekday()
                if st == en:  # 起止相同＝整天
                    hit = True
                elif st < en:
                    hit = wd in days and st <= cur < en
                else:  # 跨零点：22:00→04:00，凌晨那段算前一天的星期
                    hit = (wd in days and cur >= st) or ((wd - 1 if wd > 1 else 7) in days and cur < en)
                if hit:
                    return True
            else:
                s = str(w.get("start_at") or "").replace("Z", "+00:00")
                e = str(w.get("end_at") or "").replace("Z", "+00:00")
                if not (s and e):
                    continue
                sdt, edt = datetime.fromisoformat(s), datetime.fromisoformat(e)
                if sdt.tzinfo is None:
                    sdt = sdt.replace(tzinfo=now.tzinfo)
                if edt.tzinfo is None:
                    edt = edt.replace(tzinfo=now.tzinfo)
                if sdt <= now <= edt:
                    return True
        except Exception:
            continue
    return False


def _refresh_hint(updated: int, denied: int, no_bot: int, failed: list, first_error: str,
                  skipped: int = 0) -> str:
    """把拉取结果翻译成一句人话，给页面直接显示。"""
    tail = f"；{skipped} 个群已有名字，未重复请求接口" if skipped else ""
    if not (updated or denied or failed or no_bot):
        return (f"没有需要拉取的群{tail}" if skipped else "没有需要拉取的群")
    if updated and not denied and not failed:
        return f"已更新 {updated} 个群名{tail}"
    if denied:
        return (f"更新 {updated} 个群名；{denied} 个群取不到名字——群信息接口（/v2/groups/{{openid}}/info）"
                f"仅白名单机器人可用（错误码 11253，需向平台运营申请），这些群可在页面里手动备注{tail}")
    if no_bot:
        return f"更新 {updated} 个群名；{no_bot} 个群未能确定所属机器人，无法拉取{tail}"
    if failed:
        return f"更新 {updated} 个群名；{len(failed)} 个失败：{first_error[:120]}{tail}"
    return f"没有需要拉取的群{tail}"


class QQGroupManager(Star):
    def _check_plugin_set(self) -> None:
        """检查全局插件白名单，只告警、不写盘。

        `plugin_set`（cmd_config.json）是 AstrBot 的全局插件白名单：非 ["*"] 时
        waking_check 会按它过滤 handler，名单外的插件所有指令被静默跳过。
        早先版本在这里自动把自己的名字补进去并 save_config()，虽然能自愈"改名后指令全失效"，
        但那是插件去改 AstrBot 全局配置 —— 用户没法把它从名单里拿掉（等于关不掉），
        多插件并发写还有互相覆盖的风险。现在改为只提示，由用户去面板处理。
        """
        try:
            get_conf = getattr(self.context, "get_config", None)
            conf = get_conf() if callable(get_conf) else getattr(self.context, "astrbot_config", None)
            if conf is None:
                return
            names = conf.get("plugin_set")
            if not isinstance(names, list) or not names or "*" in names:
                return
            me = str(getattr(self, "name", "") or "").strip()
            if me and me not in names:
                self.logger.warning(
                    f"全局配置 plugin_set（插件白名单）中没有 {me}，本插件指令会被静默跳过。"
                    f"请到 AstrBot 面板的「配置」里把 {me} 加入 plugin_set；插件不会自行改全局配置。"
                )
            if LEGACY_PLUGIN_NAME != me and LEGACY_PLUGIN_NAME in names:
                self.logger.warning(
                    f"plugin_set 里残留旧插件名 {LEGACY_PLUGIN_NAME}（该目录已不存在），可在面板手动删除。"
                )
        except Exception:
            pass

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.logger = PluginLogger(
            max_entries=int(self._read_config_value(("log", "log_max_entries"), default=300, legacy_keys=("log_max_entries",))),
            level=str(self._read_config_value(("log", "log_level"), default="DEBUG", legacy_keys=("log_level",))),
            to_file=bool(self._read_config_value(("log", "log_to_file"), default=True, legacy_keys=("log_to_file",))),
            keep_days=int(self._read_config_value(("log", "log_keep_days"), default=14, legacy_keys=("log_keep_days",))),
            log_dir=self._plugin_data_dir(),
            plugin_name=PLUGIN_NAME,
        )
        self._check_plugin_set()
        self._schema_cache: dict | None = None  # _conf_schema.json 缓存（设置页用）
        self._apis: dict[str, QQApi] = {}
        self._known_groups: dict[str, str] = {}  # group_openid -> platform_id
        self._intro_done: dict[str, str] = {}    # 已发过群管自我介绍的群 -> event/message/manual
        self._intro_pending: dict[str, float] = {}  # 收到入群事件、但发送尚未确认成功的群
        self._nick_cache: dict[str, dict[str, str]] = {}  # group_openid -> {member_openid: 昵称}
        # 被 40034105（该群未开主动消息权限）拒过的群，仅用于日志/诊断提示，不阻止重试
        self._member_perm_blocked: set[str] = set()
        self._pending: dict[str, list[dict]] = {}  # group_openid -> [join_request]
        self._notified: dict[str, dict[tuple, float]] = {}  # group_openid -> {申请键: 通知时间}（防重复推送）
        # 轮询任务自己维护"上次看到的申请键"，不依赖共享的 self._pending：
        # _pending 会被 /审批 指令、WebUI /requests 接口更新，若拿它当"上一轮"
        # 对比，申请会被提前"见过"，导致新申请检测永远为空、通知永远不发送。
        self._seen_requests: dict[str, set] = {}  # group_openid -> {申请键}
        # 机器人不是群管理员（11703）时轮询入群申请必然失败：按群退避，
        # 避免每 30 秒刷一条 ERROR 把日志刷到几 MB。
        self._join_error_code: dict[str, int | None] = {}   # group_openid -> 最近一次错误码
        self._join_backoff: dict[str, float] = {}           # group_openid -> 下次允许轮询的时间戳
        self._join_denied_logged: set[str] = set()          # 已提示过 11703 的群
        # ---- 1.4.0 入群申请事件化 + 消息按钮 ----
        # GROUP_JOIN_REQUEST 事件到达时间（按群）：用来判断「事件通道是否真的在工作」，
        # 以及 event_first 模式下轮询要不要兜底。
        self._join_event_at: dict[str, float] = {}
        # 事件总数 / 最近一次全局时间（面板「概览」展示用）
        self._join_event_total = 0
        self._join_event_last = 0.0
        # 互动事件（按钮点击）去重：同一个 interaction 只处理一次。
        # 官方「同一 interaction_id 只能回应一次」，但用户可能连点，这里做幂等。
        self._interaction_done: dict[str, float] = {}
        # 互动事件统计（面板展示）
        self._interaction_total = 0
        self._interaction_last = 0.0
        self._interaction_last_result = ""
        # 申请事件合并队列：同一群 2 秒内到达的多个申请合并成一条通知，
        # 避免「三个人同时申请 → 群里刷三条消息」。
        self._join_fresh: dict[str, list[dict]] = {}
        self._join_notify_tasks: dict[str, asyncio.Task] = {}
        self._member_event_ok = False  # 群成员事件注入是否已成功
        self._last_inject_attempt = 0.0  # 最近一次注入尝试时间戳（防频繁重连）
        self._mute_history: dict[str, list[dict]] = {}  # group_openid -> [记录]
        self._history_seq = 0
        self._recall_history: dict[str, list[dict]] = {}  # group_openid -> [撤回记录]
        self._recall_seq = 0
        # 消息模板的发送格式（text / markdown / image）与图片地址。
        # 注意：模板"内容"仍保存在 AstrBot 配置里（与原生面板兼容、老配置零迁移），
        # 这里只记录"这段内容该怎么发"，属于纯展示层元数据，丢了也只是退化成纯文本。
        self._tpl_formats: dict[str, dict] = self._load_tpl_formats()
        # 入群欢迎去重：{"群openid:成员openid": 时间戳}。
        # 同一个入群事件可能被重推（断线重连补事件）；审批通过路径已经发过
        # 欢迎语时，紧随其后的 GROUP_MEMBER_ADD 也不该再发一遍。
        self._join_welcomed: dict[str, float] = {}
        # 群规审核（/举报）：数据按群隔离、全部本地落盘；
        # 注意：只有收到 /举报 才会调用模型，平时不扫描、不判定、不执行。
        self.mod = moderation.ModerationStore(self._plugin_data_dir(), self.logger)
        try:
            self.mod.verdict_max_records = int(
                self._read_config_value(("moderation", "verdict_max_records"), default=5000,
                                        legacy_keys=("verdict_max_records",)) or 0
            )
        except Exception:
            self.mod.verdict_max_records = 5000
        # 面板/配置页改了上限后不必重载：每次追加时读一次（内存字典读取，成本可忽略）
        self.mod.verdict_limit_provider = lambda: int(
            self._read_config_value(("moderation", "verdict_max_records"), default=5000,
                                    legacy_keys=("verdict_max_records",)) or 0
        )
        # 群成员档案（openid -> 昵称/备注）：官方事件与消息里只有 openid、没有昵称，
        # 所以"这个人是谁"必须落一张可人工维护的表；退群通知、禁言列表都从这取名。
        self.members = member_registry_lib.MemberRegistry(
            self._plugin_data_dir(), self.logger
        )
        # 关键词词库：内置的是公开开源词库（lexicons/SOURCES.md 有来源与协议），
        # 页面可上传自己的词库、勾选启用、加白名单；词库只做关键词维度，不单独调模型。
        self.lexicons = lexicon_lib.LexiconManager(
            self._plugin_data_dir(), plugin_dir=os.path.dirname(os.path.abspath(__file__)), logger=self.logger
        )
        self.logger.info(
            "[词库] 内置词库仅 2 个（广告引流 / 辱骂攻击，来自公开开源词表，见 lexicons/SOURCES.md），"
            "只用于本机内容过滤与拦截，不对外发布、不代表作者立场；词条内容不在面板展示。"
            "其他类型词表请自行上传或运行 tools/build_lexicons.py 生成"
        )
        # 变体检测（谐音/形近/插符号绕过）：只产出「疑似」信号，命中只撤回+记录，永不自动禁言。
        # 拼音表与形近表都是离线数据文件，缺失时自动降级（不抛异常）。
        self.variant = variant_lib.VariantMatcher(
            os.path.dirname(os.path.abspath(__file__)), self.logger
        )
        self._variant_words: Optional[list[tuple[str, tuple]]] = None  # 词条缓存（签名比对在建表侧做）
        self._mod_pending: set[str] = set()  # 正在审核的消息 ID，防重复受理
        # 模型调用闸门：同群并发 1，排队数超过 llm_queue_limit 的消息直接跳过模型层，
        # 避免刷屏群把 provider 打满、把每日额度一瞬间烧完。
        self._mod_llm_sem: dict[str, asyncio.Semaphore] = {}
        self._mod_llm_pending: dict[str, int] = {}
        self._mod_last_llm: dict = {}  # 最近一次模型调用摘要（/群管 自检 用）
        self._mod_few_shot_base: str = "".join(
            (f"- {x}" + chr(10)) for x in [
                "「加我vx：abc123 进群领福利」→ violation=true, basis=keyword, category=ad, severity=high",
                "「你这个傻X」→ violation=true, basis=semantic, category=abuse, severity=medium",
                "「哈哈哈哈哈 这也太逗了」→ violation=false（无意义刷屏但无恶意，不判违规）",
                "「楼上说得对，我刚才也这么想」→ violation=false（正常讨论）",
                "（图片为一串二维码+文字“扫码进群领红包”）→ violation=true, basis=vision, category=ad, severity=high",
            ]
        )
        # 最近收到的群消息缓存：group_openid -> [{id, text, ts, sender}]。
        # QQ 官方引用消息事件不提供被引用消息的 d.id（只有 REFIDX 引用索引，
        # 撤回 API 不认），因此引用撤回时用"原文匹配"找回真实消息 ID。
        self._recent_group_msgs: dict[str, list[dict]] = {}
        self._poll_task: Optional[asyncio.Task] = None
        # 定时汇报（日报）后台任务与"今天已经发过"的日期标记
        self._report_task: Optional[asyncio.Task] = None
        self._report_last_day: str = ""
        # 最近一次有活动的群（日报默认发到这里；在 _record_recent_msg 里更新）
        self._last_active_group: str = ""
        self._initialized = False
        # 防重入：同一消息可能被多个正则 handler 命中（如 /禁言 与 /禁言历史），
        # 用消息 ID 在短时间内去重，避免重复执行导致重复回复。
        self._last_cmd_msg_id: str = ""
        self._last_cmd_ts: float = 0.0

        self._load_known_groups()
        self._load_intro_state()
        self._load_mute_history()
        self._load_recall_history()
        self._load_report_state()
        self._register_web_apis()
        self._rebuild_mod_few_shot()
        self.logger.info(
            f"QQ官机群管 已加载 (log_level={self._read_config_value(('log', 'log_level'), default='DEBUG', legacy_keys=('log_level',))}, "
            f"poll={self._read_config_value(('approval', 'poll_interval'), default=30, legacy_keys=('poll_interval',))}s)"
        )

    # ------------------------------------------------------------------
    # 路径 / 持久化
    # ------------------------------------------------------------------
    def _plugin_data_dir(self) -> str:
        # 走 AstrBot 公开接口 StarTools.get_data_dir()：落点 data/plugin_data/<插件id>，
        # 与卸载清理、WebUI 一致。取不到时退回 AstrBot 工作目录下的同一相对路径。
        try:
            base = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            base = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_NAME)
        self._migrate_legacy_data_dir(base)
        Path(base).mkdir(parents=True, exist_ok=True)
        return base

    def _migrate_legacy_data_dir(self, new_dir: str) -> None:
        """把旧版数据目录（plugin_data/qq_group_manager）整体迁移到插件 id 目录。

        只在「旧目录存在且新目录不存在」时搬一次，搬完旧目录即消失；
        迁移后 AstrBot 卸载插件时按 <插件id> 删除，持久化数据才会真的被清掉。
        """
        if getattr(self, "_legacy_data_migrated", False):
            return
        self._legacy_data_migrated = True
        try:
            if PLUGIN_NAME == LEGACY_PLUGIN_NAME:
                return
            legacy = os.path.join(os.path.dirname(new_dir), LEGACY_PLUGIN_NAME)
            if os.path.isdir(legacy) and not os.path.exists(new_dir):
                os.replace(legacy, new_dir)
                logger.info(f"数据目录已迁移：{LEGACY_PLUGIN_NAME} -> {PLUGIN_NAME}")
        except Exception:
            pass

    def _groups_file(self) -> str:
        return os.path.join(self._plugin_data_dir(), "known_groups.json")

    def _load_known_groups(self) -> None:
        try:
            p = self._groups_file()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._known_groups = data
                    self._initialized = True
                    self.logger.info(f"加载已知群列表: {len(data)} 个群")
        except Exception as e:
            self.logger.warning(f"加载已知群列表失败: {e}")
        if not self._known_groups:
            self._initialized = False

    def _save_known_groups(self) -> None:
        try:
            _write_json_atomic(self._groups_file(), self._known_groups)
        except Exception as e:
            self.logger.warning(f"保存已知群列表失败: {e}")

    # ------------------------------------------------------------------
    # 群管自我介绍（GROUP_ADD_ROBOT 事件优先，首条消息欢迎兜底）
    #   done    : 已经发过的群 -> 发送途径（event / message / manual）
    #   pending : 收到过入群事件、但发送没成功确认的群（留给旧方案兜底）
    # ------------------------------------------------------------------
    def _intro_state_path(self) -> str:
        return os.path.join(self._plugin_data_dir(), "group_intro_state.json")

    def _load_intro_state(self) -> None:
        try:
            with open(self._intro_state_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:                                   # 坏了不能拖垮启动
            self.logger.warning(f"[入群事件] 自我介绍状态读取失败: {e}")
            return
        if not isinstance(data, dict):
            return
        self._intro_done = {str(k): str(v) for k, v in (data.get("done") or {}).items()}
        pending: dict[str, float] = {}
        for k, v in (data.get("pending") or {}).items():
            try:
                pending[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        self._intro_pending = pending

    def _save_intro_state(self) -> None:
        try:
            _write_json_atomic(
                self._intro_state_path(),
                {"done": self._intro_done, "pending": self._intro_pending},
            )
        except Exception as e:
            self.logger.warning(f"[入群事件] 自我介绍状态保存失败: {e}")

    def _mark_intro_done(self, group: str, via: str) -> None:
        """标记本群的自我介绍已发出 -> 旧方案（首条消息欢迎）自动撤销。"""
        self._intro_done[group] = via
        self._intro_pending.pop(group, None)
        self._save_intro_state()

    async def _try_fetch_group_name(self, group: str, pid: str) -> str:
        """入群时顺手拉一次群名。失败不抛异常、不重试（面板「拉取群名」可兜底）。"""
        try:
            api = self._get_api(pid)
            data, err = await api.fetch_group_info(group)
        except Exception as e:                                   # noqa: BLE001
            self.logger.debug(f"[入群事件] 拉群名异常: {e}")
            return ""
        if data and data.get("group_name"):
            name = str(data["group_name"])[:60]
            self.mod.set_group_meta(group, name=name, platform_id=pid,
                                    name_state="ok", name_error="")
            return name
        code = getattr(err, "code", None)
        self.mod.set_group_meta(group, platform_id=pid,
                                name_state="denied" if code == 11253 else "error",
                                name_error=str(err or "未知错误")[:200])
        return ""

    async def _send_group_intro(self, group: str, pid: str, event_id: str = "") -> bool:
        """发送群管功能介绍。带 event_id 时走被动消息（入群事件专用，不受 11251 限制）。"""
        text = self._render(
            "msg_group_intro",
            bot_name=pid,
            group_name=(self.mod.get_group_meta(group).get("name") or group),
        )
        if not text.strip():
            self.logger.info("[入群事件] 群管自我介绍文案为空，跳过")
            return False
        try:
            api = self._get_api(pid)
            await api.send_group_message(group, text, event_id=event_id)
        except Exception as e:                                   # noqa: BLE001
            self.logger.warning(f"[入群事件] 群管功能介绍发送失败: {e}")
            return False
        self.logger.info(f"[入群事件] 已发送群管功能介绍: group={group} 被动={bool(event_id)}")
        return True

    async def _on_group_add_robot(self, data, pid: str) -> None:
        """GROUP_ADD_ROBOT：机器人被拉进某个群。

        入群瞬间就能拿到 group_openid（不用等群里说第一句话），并用事件 id 做被动回复。
        发送成功 -> 本群旧方案自动撤销；发送失败 -> 留 pending，等首条消息兜底。
        """
        group = str(getattr(data, "group_openid", "") or "")
        event_id = str(getattr(data, "event_id", "") or "")
        if not group:
            return
        op = str(getattr(data, "op_member_openid", "") or "")
        self.logger.info(
            f"[入群事件] 机器人被加入群 {group}"
            + (f"（操作人={op[:10]}…）" if op else "")
        )

        # ① 立刻登记群（写进 known_groups，不做静默频繁拉取）
        if self._known_groups.get(group) != pid:
            self._known_groups[group] = pid
            self._save_known_groups()

        # ② 群名只拉这一次
        name = await self._try_fetch_group_name(group, pid)
        if name:
            self.logger.info(f"[入群事件] 已记录群名: {name}")

        # ③ 自我介绍：事件优先，失败留给旧方案
        if group in self._intro_done:
            self.logger.info(f"[入群事件] 本群已发过自我介绍（{self._intro_done[group]}），跳过")
            return
        # 「本群还没发过自我介绍」——开关关着也要记：事件路径不发时，
        # 首条消息那边才有办法认出这个群、并按旧方案自己的开关兜底。
        self._intro_pending[group] = time.time()
        self._save_intro_state()
        if not bool(self._read_config_value(("basic", "enable_group_add_intro"), default=True,
                                            legacy_keys=("enable_group_add_intro",))):
            self.logger.info("[入群事件] 群管自我介绍已关闭，交给首条消息兜底（受旧开关约束）")
            return
        if await self._send_group_intro(group, pid, event_id=event_id):
            self._mark_intro_done(group, "event")
        else:
            self.logger.warning(f"[入群事件] 自我介绍未发出，等本群首条消息用旧方案兜底: {group}")

    # ------------------------------------------------------------------
    # 机器人被移出群：GROUP_DEL_ROBOT 事件 + bot_state 兜底校验
    #   事件是官方在「机器人被移出群」瞬间推过来的，最权威；
    #   GET /v2/groups/{group_openid}/bot_state 用来二次确认（以及给
    #   /群管 体检 手动巡检用）。两者都不依赖群里有新消息。
    # ------------------------------------------------------------------
    async def _on_group_del_robot(self, data, pid: str) -> None:
        """GROUP_DEL_ROBOT：机器人被移出群（被踢／主动退群都算）。

        数据回收是主目的：群都没了，成员档案、群规、判定记录、禁言/撤回历史
        留着只会让面板越堆越乱，openid 也认不出是谁。
        """
        group = str(getattr(data, "group_openid", "") or "")
        if not group:
            return
        op = str(getattr(data, "op_member_openid", "") or "")
        ts = getattr(data, "timestamp", "")
        self.logger.info(
            f"[退群事件] 机器人被移出群 {group}"
            + (f"（操作人={op[:10]}…）" if op else "")
            + (f" timestamp={ts}" if ts else "")
        )

        if not self._purge_enabled():
            self.logger.info(
                "[退群事件] 自动清理已关闭（basic.enable_group_del_purge），"
                "仅记录日志，不动数据"
            )
            return

        # bot_state 二次确认：接口可用时以它为准（能挡掉误推事件），
        # 接口不可用（11253 无权限／网络错）就以事件为准 —— 事件本身已经够权威。
        state, detail = await self._check_group_presence(group, pid)
        if state == "in":
            self.logger.warning(
                f"[退群事件] bot_state 显示机器人仍在群（{detail}），"
                f"疑似误推或已被重新拉回，本次跳过清理: {group}"
            )
            return
        self.logger.info(f"[退群事件] bot_state 校验结果：{detail}")

        summary = self._purge_group_data(
            group, pid, reason=f"机器人被移出群（操作人={op[:10] or '未知'}）"
        )
        self.logger.info(
            f"[退群事件] 已清理群 {group} 的数据：" + _purge_summary_text(summary)
        )

    def _purge_enabled(self) -> bool:
        return bool(
            self._read_config_value(
                ("basic", "enable_group_del_purge"),
                default=True,
                legacy_keys=("enable_group_del_purge",),
            )
        )

    async def _check_group_presence(
        self, group: str, pid: str = ""
    ) -> tuple[str, str]:
        """用 bot_state 接口确认机器人还在不在这个群。

        Returns:
            (state, detail)，state 取值：

            - ``in``      —— 接口正常返回了机器人在群的信息；
            - ``absent``  —— 接口明确报「查不到这个群／不在群里」（404、
              `11293 鉴权失败，机器人非群成员`，或文案含「非群成员」等）；
            - ``unknown`` —— 没权限（11253，该接口仅白名单机器人可用）、
              网络失败、频控等，**一律不当作离群**，避免误删用户数据。
        """
        group = str(group or "")
        if not group:
            return "unknown", "群 openid 为空，无法查询"
        try:
            api = self._get_api(pid) if pid else self._api_for_group_or_default(group, "")
        except Exception as e:                       # noqa: BLE001
            return "unknown", f"拿不到 API 客户端: {e}"
        if api is None:
            return "unknown", "没有可用的机器人实例"
        try:
            info, err = await api.get_group_bot_state(group)
        except Exception as e:                       # noqa: BLE001
            return "unknown", f"调用 bot_state 异常: {e}"

        if err is None:
            if not info:
                return "in", "接口无错误但返回空对象（视为在群）"
            role = str(info.get("member_role") or "?")
            joined = str(info.get("joined_at") or "?")
            proactive = info.get("allow_proactive_msg")
            return "in", f"在群（role={role}，入群时间={joined}，主动消息={proactive}）"

        code = getattr(err, "code", None)
        status = getattr(err, "status", None)
        text = str(err)
        if code == 11253:
            return (
                "unknown",
                "bot_state 接口无权限（11253：仅白名单机器人可用），无法确认，按事件为准",
            )
        # 11293 = 「鉴权失败，机器人非群成员」：这是**明确离群**，不是无权限。
        # 平台用 400 而非 404 返回它，且文案里没有「不存在」，早期只看关键词会误判成 unknown。
        if code == 11293:
            return "absent", f"机器人非群成员（{text}）"
        lowered = text.lower()
        absent_hint = (
            status == 404
            or any(
                k in text
                for k in (
                    "不在群", "未在群", "非群成员", "不是群成员", "群不存在", "不存在",
                )
            )
            or any(
                k in lowered
                for k in (
                    "not found", "not in group", "not a member",
                    "not a group member", "invalid group",
                )
            )
        )
        if absent_hint:
            return "absent", f"不在群（{text}）"
        return "unknown", f"查询失败，无法确认（{text}）"

    def _purge_group_data(self, group: str, pid: str = "", reason: str = "") -> dict:
        """把一个群在本插件里的所有痕迹清掉，返回各表删除数量。

        覆盖：已知群列表、群管自我介绍状态、禁言历史、撤回历史、成员档案、
        群规/审核设置/判定记录/群名备注/提醒名单、入群欢迎去重、各种
        按群缓存与待推送任务。
        """
        group = str(group or "")
        if not group:
            return {}
        reason = reason or "手动清理"
        out: dict[str, object] = {}

        # ① 已知群列表
        if self._known_groups.pop(group, None) is not None:
            self._save_known_groups()
            out["known_groups"] = 1

        # ② 群管自我介绍状态（发过的标记 / 待兜底标记）
        touched = False
        if self._intro_done.pop(group, None) is not None:
            touched = True
        if self._intro_pending.pop(group, None) is not None:
            touched = True
        if touched:
            self._save_intro_state()

        # ③ 禁言历史 / 撤回历史
        if self._mute_history.pop(group, None) is not None:
            self._save_mute_history()
            out["mute_history"] = 1
        if self._recall_history.pop(group, None) is not None:
            self._save_recall_history()
            out["recall_history"] = 1

        # ④ 成员档案
        try:
            n = self.members.purge_group(group)
            if n:
                out["members"] = n
        except Exception as e:                       # noqa: BLE001
            self.logger.warning(f"[清理] 成员档案清理失败: {e}")

        # ⑤ 群规 / 审核设置 / 判定记录 / 群名备注 / 提醒名单 / 群模板
        try:
            removed = self.mod.purge_group(group)
            if any(int(v or 0) for v in removed.values()):
                out.update({k: v for k, v in removed.items() if int(v or 0)})
        except Exception as e:                       # noqa: BLE001
            self.logger.warning(f"[清理] 审核数据清理失败: {e}")

        # ⑥ 入群欢迎去重表（键是 "群:成员"）
        prefix = f"{group}:"
        stale = [k for k in self._join_welcomed if k.startswith(prefix)]
        for k in stale:
            self._join_welcomed.pop(k, None)
        if stale:
            out["welcome_dedupe"] = len(stale)

        # ⑦ 按群内存缓存 / 待推送任务
        for attr in (
            "_nick_cache", "_pending", "_notified", "_seen_requests",
            "_join_error_code", "_join_backoff", "_join_event_at",
            "_join_fresh", "_recent_group_msgs",
        ):
            bucket = getattr(self, attr, None)
            if isinstance(bucket, dict) and bucket.pop(group, None) is not None:
                out[attr.lstrip("_")] = 1
        for attr in ("_member_perm_blocked", "_join_denied_logged"):
            bucket = getattr(self, attr, None)
            if isinstance(bucket, set):
                bucket.discard(group)
        task = self._join_notify_tasks.pop(group, None)
        if task is not None and not task.done():
            task.cancel()
            out["notify_task"] = 1
        if self._last_active_group == group:
            self._last_active_group = ""

        self.logger.info(f"[清理] 群 {group} 数据已清理（{reason}）：{_purge_summary_text(out)}")
        return out

    async def _cmd_status(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """/群管 状态 [日报|群|自检|群名]：所有「看一眼现状」的指令收在这一个入口。

        - 日报（默认，原 /群管 汇报）：待处理申请 + 当前禁言人数
        - 群（原 /群管 体检 [清理]）：机器人还在不在已记录的群里
        - 自检（原 /群管 自检）：审核链路配置与今日用量
        - 群名（原 /群管 群名）：刷新本群群名（面板群列表用）
        """
        sub = parts[1].lower() if len(parts) > 1 else ""
        args = parts[2:]
        if sub in ("", "日报", "汇报", "daily", "摘要", "summary"):
            await self._cmd_report(event)
        elif sub in ("群", "体检", "presence", "在群"):
            await self._cmd_presence(event, ["/体检"] + args)
        elif sub in ("自检", "诊断", "selfcheck", "diag"):
            await self._cmd_selfcheck(event)
        elif sub in ("群名", "名称", "name", "刷新群名"):
            await self._cmd_group_name(event)
        else:
            await self._reply(
                event,
                "用法：/群管 状态 [日报 | 群 [清理] | 自检 | 群名]\n"
                "· 日报＝待处理申请 + 禁言人数\n"
                "· 群＝机器人还在不在已记录的群里\n"
                "· 自检＝审核链路配置与今日用量\n"
                "· 群名＝刷新本群群名",
            )

    def _is_bot_admin_only(self, event: AstrMessageEvent) -> bool:
        """是不是「机器人管理员」（AstrBot 管理员 / 自定义 OpenID 名单）。

        只用于「群管理员不能撤销针对自己的处罚」这条判定：机器人管理员不受限。
        """
        sid = event.get_sender_id()
        try:
            if bool(self._read_config_value(("admin", "admin_use_astrbot_admin"), default=True,
                                           legacy_keys=("admin_use_astrbot_admin",))):
                admins = (self.context.get_config() or {}).get("admins_id") or []
                if sid in admins or getattr(event, "role", "") == "admin":
                    return True
        except Exception:
            pass
        extra = self._read_config_value(("admin", "admin_openids"), default=[],
                                        legacy_keys=("admin_openids",)) or []
        return sid in extra

    async def _revoke_verdict(self, group: str, vid: int, *, operator: str = "",
                              operator_name: str = "", reason: str = "",
                              is_bot_admin: bool = False) -> dict:
        """撤销一条已生效的处罚：留审计 → 尝试解禁 → 从「前科统计」中排除。

        解禁以**接口真值**为准（get_mute_state），不信本地禁言记录：
        群管手动解禁、自然到期、机器人重启都会让本地记录漂移。
        """
        rec = self.mod.get_verdict(vid)
        if rec is None:
            return {"ok": False, "error": "not_found"}
        if str(rec.get("group") or "") != str(group):
            return {"ok": False, "error": "cross_group"}
        if rec.get("revoked"):
            return {"ok": True, "already": True, "record": rec}
        if not rec.get("violation") or not (
            rec.get("executed") or str(rec.get("action") or "") in ("warn", "recall", "mute")
        ):
            return {"ok": False, "error": "not_punished"}
        target = str(rec.get("target") or "")
        if not is_bot_admin and target and target == str(operator):
            return {"ok": False, "error": "self_revoke"}

        unmute = {"needed": False, "ok": None, "code": ""}
        muted = bool(rec.get("ban_seconds")) or str(rec.get("action") or "") == "mute"
        if target and muted:
            unmute["needed"] = True
            try:
                api = self._api_for_group_or_default(group)
                state = await api.get_mute_state(group)
                members = state.get("members") or []
                still = any(str(m.get("member_openid") or "") == target for m in members)
                if still:
                    ok, err = await self._run_action(api.set_mute(group, target, op="del"))
                    if not ok:
                        raise RuntimeError(err)
                    self._mark_unmuted(group, target)
                    try:                                     # 复查，别"看起来成功了"
                        state2 = await api.get_mute_state(group)
                        still = any(str(m.get("member_openid") or "") == target
                                    for m in (state2.get("members") or []))
                    except Exception:
                        still = False
                    unmute["ok"] = not still
                else:
                    unmute["ok"] = True                      # 本来就已经不在了（手动/到期）
            except Exception as e:
                unmute["ok"] = False
                unmute["code"] = str(e)[:120]

        info = {
            "by": str(operator or ""),
            "by_name": str(operator_name or "")[:40],
            "at": int(time.time()),
            "reason": str(reason or "")[:200],
            "unmute_needed": unmute["needed"],
            "unmute_ok": unmute["ok"],
            "unmute_code": unmute["code"],
        }
        self.mod.update_verdict(vid, revoked=info)
        self.logger.info(
            f"[审核] 撤销处罚 id={vid} group={group} by={info['by_name'] or info['by']} "
            f"reason={info['reason']!r} 解禁={unmute}"
        )
        return {"ok": True, "record": rec, "revoke": info, "unmute": unmute}

    async def _cmd_revoke(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """撤销一条处罚记录并致歉：/群管 撤销 <记录ID> [理由]"""
        g = event.get_group_id()
        args = parts[1:]
        vid, reason = 0, ""
        for i, a in enumerate(args):
            if str(a).isdigit():
                vid = int(a)
                reason = " ".join(str(x) for x in args[i + 1:])
                break
        if not vid:
            await self._reply(event, "用法：/群管 撤销 <记录ID> [理由]\n记录ID 在面板「记录 → 判定记录」里能查到。")
            return
        rec = self.mod.get_verdict(vid)
        if rec is None:
            await self._reply(event, f"没找到记录 #{vid}。")
            return
        if str(rec.get("group") or "") != str(g):
            await self._reply(event, "只能撤销本群的处罚记录。")
            return
        if not self._is_admin(event):
            await self._reply(event, "只有群主、群管理员或机器人管理员可以撤销处罚。")
            return
        is_bot_admin = self._is_bot_admin_only(event)

        res = await self._revoke_verdict(
            g, vid,
            operator=event.get_sender_id() or "",
            operator_name=event.get_sender_name() or "",
            reason=reason,
            is_bot_admin=is_bot_admin,
        )
        if not res.get("ok"):
            reason_map = {
                "self_revoke": "不能撤销针对自己的处罚（机器人管理员除外）。",
                "not_punished": "这条记录没有实际处罚（未违规 / 试运行 / 未执行），无需撤销。",
                "cross_group": "只能撤销本群的处罚记录。",
                "not_found": f"没找到记录 #{vid}。",
            }
            await self._reply(event, reason_map.get(res.get("error"), f"撤销失败：{res.get('error')}"))
            return
        if res.get("already"):
            await self._reply(event, f"记录 #{vid} 已经在 {time.strftime('%Y-%m-%d %H:%M', time.localtime(int(res['record']['revoked'].get('at') or 0)))} 被撤销过了。")
            return

        target = str(rec.get("target") or "")
        unmute = res.get("unmute") or {}
        if unmute.get("needed"):
            unmute_txt = "已解除" if unmute.get("ok") else (
                "无需解除(当前不在禁言中)" if unmute.get("ok") is True else "解除失败，请手动解禁"
            )
        else:
            unmute_txt = "无需解除"
        await self._reply(event, self._mod_tpl(
            g,
            "msg_mod_revoke",
            vid=vid,
            at=self._at_tag(target) if target else "",
            punish=self._punish_text(rec),
            unmute=unmute_txt + (f"（{unmute.get('code')}）" if unmute.get("code") else ""),
            operator=event.get_sender_name() or event.get_sender_id(),
            reason_line=(f"\n说明：{reason}" if reason else ""),
        ))

    def _punish_text(self, rec: dict) -> str:
        """把一条记录的原处罚描述成人话，供撤销回执使用。"""
        action = str(rec.get("action") or "")
        secs = int(rec.get("ban_seconds") or 0)
        if action == "mute" or secs:
            return f"禁言 {fmt_duration(secs)}（记录 #{rec.get('id')}）"
        if action == "recall" or rec.get("executed"):
            return f"撤回消息（记录 #{rec.get('id')}）"
        if action == "warn":
            return f"警告（记录 #{rec.get('id')}）"
        return f"无处罚（记录 #{rec.get('id')}）"

    async def _cmd_selfcheck(self, event: AstrMessageEvent) -> None:
        """/群管 自检：一眼看清审核链路「配没配好、调没调上、花了多少」。

        刻意不做主动探活——探活等于真发一次请求，既花钱又要等；
        要看真调用的细节，把日志级别调到 DEBUG 看 [模型] 行即可。
        """
        g = event.get_group_id()
        st = self._mod_settings(g)
        try:
            pid = str(await self._mod_provider_id(event, group=g) or "")
        except Exception as e:                                  # provider 解析失败本身就是要报的问题
            pid = f"解析失败（{e}）"
        try:
            stats = self.mod.stats(g) or {}
        except Exception:
            stats = {}
        last = dict(self._mod_last_llm or {})
        budget = int(st.get("daily_budget") or 0)
        used = int(stats.get("today_llm_calls", 0) or 0)
        queue = int(st.get("llm_queue_limit") or 0)
        timeout = int(st.get("llm_timeout") or 0)
        if last:
            last_line = (f"最近一次模型调用：{last.get('scene') or '-'} "
                         f"{'成功' if last.get('ok') else '失败'} {last.get('ms', '?')}ms"
                         + (f"（{last.get('error')}）" if last.get("error") else ""))
        else:
            last_line = "最近一次模型调用：本进程内还没有（近期没走到模型层）"
        lines = [
            "【群管自检】",
            f"适配器：{event.get_platform_id() or '-'}",
            f"审核开关：{'开' if st.get('enabled') else '关'}"
            f"｜执行模式：{'试运行（只判定不执行）' if st.get('dry_run') else '真实执行'}",
            f"分层：关键词 {'开' if st.get('enable_keyword') else '关'}"
            f"／模型 {'开' if st.get('enable_model') else '关'}"
            f"／图片 {'开' if st.get('enable_vision') else '关'}",
            f"审核模型：{pid or '未配置（模型层会直接跳过，只跑关键词）'}",
            f"今日模型调用：{used}" + (f" / 预算 {budget}" if budget > 0 else "（未设上限）"),
            f"累计判定 {stats.get('total', 0)} 条"
            f"｜本次统计以来缓存命中率 {round(float(stats.get('cache_rate') or 0) * 100)}%",
            f"并发闸门：排队 {self._mod_llm_pending.get(g, 0)}/{queue or '不限'}"
            f"｜单次超时 {timeout or '不限'}s",
            last_line,
            "提示：不做主动探活（会真花钱）；想看调用细节，把日志级别调到 DEBUG 看 [模型] 行。",
        ]
        await self._reply(event, "\n".join(lines))

    async def _cmd_presence(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """/群管 体检：逐个确认机器人还在不在已记录的群里。

        默认只报告；带 ``清理`` 才真删数据（避免一次手滑清一堆群）。
        bot_state 需要白名单权限，拿不到权限的群一律标「无法确认」，不动数据。
        """
        do_purge = any(str(p).lower() in ("清理", "purge", "clean") for p in parts[1:])
        cur = event.get_group_id()
        groups = dict(self._managed_groups())
        if cur and cur not in groups:
            groups[cur] = event.get_platform_id()

        if not groups:
            await self._reply(event, "还没有记录到任何群，等机器人在群里收到消息或收到入群事件后再说。")
            return

        lines = [f"【群管体检】已知群 {len(groups)} 个" + ("（检出离群会自动清理）" if do_purge else "（只报告，不改数据；加「清理」才会删）")]
        stat = {"in": 0, "absent": 0, "unknown": 0}
        purged = 0
        for g, pid in list(groups.items())[:20]:      # 上限 20 个，避免回执太长
            state, detail = await self._check_group_presence(g, pid)
            stat[state] = stat.get(state, 0) + 1
            label = self.mod.display_name(g) or g
            tail = f"…{g[-6:]}"
            if state == "in":
                lines.append(f"✅ {label}（{tail}）{detail}")
            elif state == "absent":
                if do_purge and self._purge_enabled():
                    summary = self._purge_group_data(g, pid, reason="/群管 体检 检出离群")
                    purged += 1
                    lines.append(f"❌ {label}（{tail}）{detail}，已清理：{_purge_summary_text(summary, empty='无残留数据')}")
                else:
                    lines.append(f"❌ {label}（{tail}）{detail}" + ("（加「清理」可删除本群数据）" if not do_purge else "（自动清理开关已关，未删除）"))
            else:
                lines.append(f"⚠️ {label}（{tail}）{detail}")
        if len(groups) > 20:
            lines.append(f"……还有 {len(groups) - 20} 个群没检查（一次最多 20 个，可分批用 /群管 成员 查看）")
        lines.append(
            f"小结：在群 {stat.get('in', 0)}｜离群 {stat.get('absent', 0)}｜无法确认 {stat.get('unknown', 0)}"
            + (f"｜已清理 {purged} 个群" if purged else "")
        )
        await self._reply(event, "\n".join(lines))

    # ---------- 禁言历史（长期存储） ----------

    def _history_file(self) -> str:
        return os.path.join(self._plugin_data_dir(), "mute_history.json")

    def _load_mute_history(self) -> None:
        try:
            p = self._history_file()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._mute_history = {
                        str(k): v for k, v in data.get("records", {}).items()
                        if isinstance(v, list)
                    }
                    self._history_seq = int(data.get("seq", 0) or 0)
                    total = sum(len(v) for v in self._mute_history.values())
                    self.logger.info(f"加载禁言历史: {total} 条（{len(self._mute_history)} 个群）")
        except Exception as e:
            self.logger.warning(f"加载禁言历史失败: {e}")
        # 到点自动解的禁言没人去改记录，启动时顺手把过了期的 active 落成 expired
        try:
            n = self._sweep_expired_mutes()
            if n:
                self.logger.info(f"禁言历史: {n} 条已到期的记录已更新为「已到期」")
        except Exception as e:
            self.logger.warning(f"刷新禁言状态失败: {e}")

    def _save_mute_history(self) -> None:
        try:
            _write_json_atomic(
                self._history_file(),
                {"seq": self._history_seq, "records": self._mute_history},
            )
        except Exception as e:
            self.logger.warning(f"保存禁言历史失败: {e}")

    # ------------------------------------------------------------------
    # 禁言状态：记录里只有 active / unmuted，到点自动解的那种要按到期时间折算
    # ------------------------------------------------------------------
    @staticmethod
    def _effective_mute_status(rec: dict, now: Optional[float] = None) -> str:
        """返回记录的实际状态。

        禁言到期是 QQ 侧自动解开的，插件不会收到通知，所以本地记录的 status 会
        永远停在 active——直接展示就会出现「都过了一天还写着禁言中」。展示前跟
        expire_at 比一下即可：过点算 expired（已到期），未过点仍然 active。
        """
        st = rec.get("status") or "active"
        if st == "active":
            exp = int(rec.get("expire_at") or 0)
            if exp and (now if now is not None else time.time()) >= exp:
                return "expired"
        return st

    def _sweep_expired_mutes(self, now: Optional[float] = None) -> int:
        """把已到期的 active 记录落库成 expired，返回改动条数。

        懒执行（启动时 + 打开禁言历史时各扫一次），不额外起后台任务；这样
        mute_history.json 里的状态也不会一直是「禁言中」。
        """
        ts = now if now is not None else time.time()
        n = 0
        for lst in self._mute_history.values():
            for rec in lst:
                if rec.get("status") == "active" and self._effective_mute_status(rec, ts) == "expired":
                    rec["status"] = "expired"
                    n += 1
        if n:
            self._save_mute_history()
        return n

    # ------------------------------------------------------------------
    # 日报状态（"今天已发过"必须落盘，否则重载插件会重复推送）
    # ------------------------------------------------------------------
    def _report_state_path(self) -> str:
        return os.path.join(self._plugin_data_dir(), "report_state.json")

    def _load_report_state(self) -> None:
        """读取上次日报日期。

        内存标记只在本次进程内有效，而 AstrBot 在 WebUI 里保存配置、重载插件
        都会重建插件实例 —— 只要当天日报已经发过，重启后 30 秒内又会再发一遍，
        群里就是重复推送。所以把日期落盘。
        """
        try:
            with open(self._report_state_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            day = str((data or {}).get("last_day") or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                self._report_last_day = day
        except FileNotFoundError:
            return
        except Exception as e:  # 坏了不影响功能，当今天没发过
            self.logger.warning(f"[日报] 状态读取失败: {e}")

    def _save_report_state(self) -> None:
        try:
            _write_json_atomic(
                self._report_state_path(), {"last_day": self._report_last_day}
            )
        except Exception as e:
            self.logger.warning(f"[日报] 状态保存失败: {e}")

    # ------------------------------------------------------------------
    # 撤回历史（永久保存原始消息，便于备份查验）
    # ------------------------------------------------------------------
    def _recall_history_file(self) -> str:
        return os.path.join(self._plugin_data_dir(), "recall_history.json")

    def _load_recall_history(self) -> None:
        try:
            p = self._recall_history_file()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._recall_history = {
                        str(k): v for k, v in data.get("records", {}).items()
                        if isinstance(v, list)
                    }
                    self._recall_seq = int(data.get("seq", 0) or 0)
                    total = sum(len(v) for v in self._recall_history.values())
                    self.logger.info(f"加载撤回历史: {total} 条（{len(self._recall_history)} 个群）")
        except Exception as e:
            self.logger.warning(f"加载撤回历史失败: {e}")

    def _save_recall_history(self) -> None:
        try:
            _write_json_atomic(
                self._recall_history_file(),
                {"seq": self._recall_seq, "records": self._recall_history},
            )
        except Exception as e:
            self.logger.warning(f"保存撤回历史失败: {e}")

    def _add_recall_record(self, group_openid: str, platform_id: str,
                           message_id: str, content: str, source: str,
                           operator: str, operator_name: str = "") -> dict:
        """记录一条撤回操作。content 为被撤回消息的原文（引用撤回时能拿到，
        直接传 message_id 时平台接口不返回内容，则留空仅保留消息 ID）。"""
        self._recall_seq += 1
        rec = {
            "id": self._recall_seq,
            "group_openid": group_openid,
            "platform_id": platform_id,
            "message_id": message_id,
            "content": content or "",
            "source": source,  # quote=引用撤回, message_id=传ID撤回
            "recalled_at": int(time.time()),
            "operator": operator,
            "operator_name": operator_name,
        }
        lst = self._recall_history.setdefault(group_openid, [])
        lst.append(rec)
        # 超限裁剪（默认 0 = 不限，永久保存；配置可设上限）
        limit = int(self._read_config_value(
            ("recall", "recall_history_max_per_group"), default=0,
            legacy_keys=("recall_history_max_per_group",)))
        if limit > 0 and len(lst) > limit:
            del lst[: len(lst) - limit]
        self._save_recall_history()
        return rec

    def _add_mute_record(self, group_openid: str, platform_id: str,
                         member_openid: str, username: str, reason: str,
                         seconds: int, expire_ts: float, operator: str,
                         operator_name: str = "") -> dict:
        """记录一条禁言。返回记录 dict。"""
        self._history_seq += 1
        rec = {
            "id": self._history_seq,
            "group_openid": group_openid,
            "platform_id": platform_id,
            "member_openid": member_openid,
            "username": username or "未知成员",
            "reason": reason or "",
            "seconds": int(seconds),
            "duration_text": fmt_duration(int(seconds)),
            "mute_at": int(time.time()),
            "expire_at": int(expire_ts),
            "operator": operator,
            "operator_name": operator_name,
            "status": "active",  # active=禁言中 unmuted=已解禁 expired=已到期
        }
        lst = self._mute_history.setdefault(group_openid, [])
        lst.append(rec)
        # 超限裁剪（保留最新的）
        if len(lst) > HISTORY_MAX_PER_GROUP:
            del lst[: len(lst) - HISTORY_MAX_PER_GROUP]
        self._save_mute_history()
        return rec

    def _last_reason(self, group_openid: str, member_openid: str) -> Optional[str]:
        """查某成员最近一次禁言的理由（用于 /查禁言 展示）。"""
        for rec in reversed(self._mute_history.get(group_openid, [])):
            if rec.get("member_openid") == member_openid and rec.get("reason"):
                return rec.get("reason")
        return None

    def _mark_unmuted(self, group_openid: str, member_openid: str) -> None:
        """解禁时把该成员最近一条 active 记录标记为已解禁。"""
        lst = self._mute_history.get(group_openid, [])
        changed = False
        for rec in reversed(lst):
            if rec.get("member_openid") == member_openid and rec.get("status") == "active":
                rec["status"] = "unmuted"
                rec["unmute_at"] = int(time.time())
                changed = True
                break
        if changed:
            self._save_mute_history()

    # ------------------------------------------------------------------
    # 身份获取 / API
    # ------------------------------------------------------------------
    def _get_api(self, platform_id: str) -> QQApi:
        """按平台动态获取 API 客户端（appid/secret 来自平台实例，不写死）。"""
        if platform_id in self._apis:
            return self._apis[platform_id]
        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            raise QQApiError(f"找不到平台实例: {platform_id}")
        cfg = getattr(platform, "config", None) or {}
        appid = cfg.get("appid")
        secret = cfg.get("secret")
        if not appid or not secret:
            raise QQApiError(f"平台 {platform_id} 未配置 appid/secret")
        api = QQApi(appid, secret, self.logger)
        self._apis[platform_id] = api
        self.logger.info(f"初始化 QQ API 客户端: platform={platform_id} appid={appid}")
        return api

    # ------------------------------------------------------------------
    # 管理员判定（多模式，WebUI 可配）
    # ------------------------------------------------------------------
    def _get_qq_role(self, event: AstrMessageEvent) -> Optional[str]:
        """尝试从事件原始数据读取 QQ 群角色。

        注意：QQ 官方（qqofficial）群消息的 author 对象只含 member_openid，
        没有 member_role 字段，因此该判定在官方通道下恒为 None（管理员判定
        主要依赖 AstrBot admins_id 或插件 admin_openids）。
        """
        try:
            raw = event.message_obj.raw_message
            author = getattr(raw, "author", None)
            if author is None:
                return None
            return (
                getattr(author, "member_role", None)
                or getattr(author, "role", None)
            )
        except Exception:
            return None

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        conds: list[bool] = []
        sender_id = event.get_sender_id()
        # 1. AstrBot 内置管理员
        if bool(self._read_config_value(("admin", "admin_use_astrbot_admin"), default=True, legacy_keys=("admin_use_astrbot_admin",))):
            c = event.role == "admin"
            try:
                admins = (self.context.get_config() or {}).get("admins_id") or []
                if sender_id in admins:
                    c = True
            except Exception:
                pass
            conds.append(c)
        # 2. QQ 群角色（兼容字符串 "owner"/"admin" 与数字 1/2）
        if bool(self._read_config_value(("admin", "admin_use_qq_role"), default=True, legacy_keys=("admin_use_qq_role",))):
            role = self._get_qq_role(event)
            self.logger.debug(f"QQ 群角色判定: sender={sender_id} role={role}")
            conds.append(str(role) in ("owner", "admin", "1", "2"))
        # 3. 自定义 OpenID 名单
        extra = self._read_config_value(("admin", "admin_openids"), default=[], legacy_keys=("admin_openids",)) or []
        if extra:
            conds.append(sender_id in extra)
        if not conds:
            self.logger.warning("管理员判定条件全部关闭，所有群管指令将被拒绝！")
            return False
        mode = str(self._read_config_value(("admin", "admin_mode"), default="any", legacy_keys=("admin_mode",)))
        result = all(conds) if mode == "all" else any(conds)
        self.logger.debug(f"管理员判定结果: {result} (mode={mode} sender={sender_id})")
        return result

    # ------------------------------------------------------------------
    # 嵌套配置读取（配合分组式 _conf_schema.json）
    # ------------------------------------------------------------------
    def _read_path(self, path: tuple[str, ...], missing=None):
        """按路径读取嵌套配置，找不到返回 missing。"""
        current = self.config
        for key in path:
            if not isinstance(current, dict) or key not in current:
                return missing
            current = current[key]
        return current

    def _read_config_value(
        self,
        primary_path: tuple[str, ...],
        default=None,
        legacy_keys: tuple[str, ...] = (),
    ):
        """优先读新分组路径，读不到回退旧扁平 key，最后用默认值。

        这样用户历史保存的扁平配置（改过的模板等）在迁移后仍能生效。
        """
        missing = object()
        value = self._read_path(primary_path, missing)
        if value is not missing and value is not None:
            return value
        for legacy_key in legacy_keys:
            value = self.config.get(legacy_key, missing)
            if value is not missing and value is not None:
                return value
        return default

    # ------------------------------------------------------------------
    # 模板渲染
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 消息模板格式（text / markdown / image）
    # ------------------------------------------------------------------
    TPL_FORMAT_KINDS = ("text", "markdown", "image")

    # 这些错误码说明「消息本体没问题、是键盘/格式参数不被接受」，
    # 遇到时去掉键盘重发一次即可保住正文：
    #   305007   键盘样式参数错误
    #   40034029 内联键盘行/列超限
    #   40034124 markdown 消息参数错误
    #   22006    消息类型与内容不匹配
    #   40011021 rows.buttons.group_id 无效（markdown 通道特有的严校验）
    KEYBOARD_DROP_CODES = {305007, 40034029, 40034124, 22006, 40011021}

    # 这些错误码跟「消息用什么格式」无关（无权限 / 频控 / 不在群 / 被禁言 /
    # 内容超长），换 markdown、换纯文本都一样失败，别再白跑降级阶梯。
    FATAL_SEND_CODES = {
        11251, 40034100, 40034101, 40034105, 40054002, 40054003, 40054007, 40054010,
    }


    # 新增模板的内置兜底文案。
    # 为什么需要：升级后 AstrBot 可能还没把新模板项写进配置文件，
    # 这时 _render 读到的模板是空串，消息会被静默丢掉。留一份兜底，
    # 老配置升级上来也能正常发（用户可在配置里改成自己的话术）。
    TPL_FALLBACKS = {
        "msg_mod_todo": "⚠️ 这条内容被判为疑似严重违规，但模型置信度不足，已转人工复核，暂不处罚。",
        "msg_mod_revoke": "✅ 已撤销处罚记录 #{vid}\n{at} 经复核，本条判定有误，已撤销对该成员的处罚并致歉。\n"
                          "原处罚：{punish}\n禁言状态：{unmute}\n撤销人：{operator}{reason_line}",
        "msg_mod_warn": "{at} ⚠️ 本消息已记录（警告，不撤回）\n"
                        "命中：{rule}\n判定：{severity}\n理由：{reason}\n"
                        "后续：再次出现将禁言 {next}\n有异议请联系群管理。",
        "msg_join_request_auto_approved": (
            "✅ 新成员 {username} 的入群申请已由自动审批策略通过（策略 {strategy}）"
        ),
        "msg_join_request_button_ok": "✅ 已通过 {username} 的入群申请",
        "msg_join_request_button_rejected": "🚫 已拒绝 {username} 的入群申请",
        "msg_join_request_button_gone": (
            "⚠️ 这条入群申请已处理或已失效（可能刚被其他人处理），无需重复操作"
        ),
        "msg_join_request_button_denied": "⛔ 你没有处理入群申请的权限，请让群主/管理员操作",
        "msg_join_request_button_failed": "❌ 处理 {username} 的入群申请失败：{error}",
        "msg_report_daily": (
            "【群管日报】{date} {time}\n待处理入群申请：{pending_count} 条\n{list}\n当前禁言：{muted_count} 人"
        ),
    }

    def _tpl_formats_path(self) -> str:
        return os.path.join(self._plugin_data_dir(), "msg_formats.json")

    def _load_tpl_formats(self) -> dict:
        path = self._tpl_formats_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.logger.warning(f"读取消息模板格式失败，全部按纯文本处理: {e}")
        return {}

    def _save_tpl_formats(self) -> None:
        path = self._tpl_formats_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._tpl_formats, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换：写一半中断也不会把已有格式文件写坏

    def _tpl_format(self, key: str) -> str:
        meta = self._tpl_formats.get(key) or {}
        kind = str(meta.get("type") or "text").strip().lower()
        if not meta and key == "msg_mod_warn":
            return "markdown"          # 警告默认 markdown；可在「消息模板」页改回文本
        return kind if kind in self.TPL_FORMAT_KINDS else "text"

    def _tpl_keys(self) -> set[str]:
        schema = self._conf_schema()
        group = schema.get("templates") if isinstance(schema.get("templates"), dict) else {}
        items = group.get("items") if isinstance(group.get("items"), dict) else {}
        return {k for k, v in items.items() if isinstance(v, dict)}

    def _render(self, key: str, **kw) -> str:
        tpl = str(self._read_config_value(("templates", key), default="", legacy_keys=(key,)))
        if not tpl.strip():
            tpl = self.TPL_FALLBACKS.get(key, "")
        # {at} 内置模块：调用方传 at=<openid> 时直接展开成 @ 标签；
        # 没传就保留占位符，交给 _reply() 用「消息发送者」兜底展开。
        target = str(kw.get("at") or "").strip()
        if target:
            kw["at"] = self._at_tag(target)
        kw = {k: (str(v) if v is not None else "") for k, v in kw.items()}
        try:
            result = tpl.format(**kw)
        except Exception:
            # 模板里有不配对的花括号时 format 会抛，退回逐个替换
            result = tpl
            for k, v in kw.items():
                result = result.replace("{" + k + "}", v)
        return _TplText(result, key, target)

    # ---- 消息内置模块：@群员 / 群员头像 ----
    # 依据 QQ 官方「文本交互」能力：
    #   @成员标签  <qqbot-at-user id="openid" />（旧的 <@openid> 官方已标注即将弃用）
    #   markdown   ![说明 #宽px #高px](url)，图片须公网可访问
    # 头像走 q.qlogo.cn 公开接口，无需鉴权、不消耗机器人配额。
    AT_TAG_FMT = '<qqbot-at-user id="{openid}" />'
    AVATAR_URL_FMT = "https://q.qlogo.cn/qqapp/{appid}/{openid}/{size}"
    # 头像源档位（qlogo 公开支持的规格）：按展示尺寸就近向上取，小图不拉大图，大图不糊
    AVATAR_SRC_TIERS = (40, 100, 640)
    _AVATAR_PH_RE = re.compile(r"\{avatar(?::(\d+))?\}")
    _LEGACY_AT_RE = re.compile(r"<@!?([0-9A-Za-z_-]{8,})>")

    def _at_tag(self, openid: str) -> str:
        """openid → QQ 官方 @ 标签；空值返回空串。"""
        oid = str(openid or "").strip().strip("<>@")
        return self.AT_TAG_FMT.format(openid=oid) if oid else ""

    def _platform_appid(self, platform_id: str) -> str:
        """取平台实例的 appid（头像 URL 需要）。取不到返回空串，绝不抛异常。"""
        try:
            platform = self.context.get_platform_inst(platform_id)
            cfg = getattr(platform, "config", None) or {}
            return str(cfg.get("appid") or "")
        except Exception:
            return ""

    def _expand_msg_modules(
        self, text: str, event: AstrMessageEvent | None, fmt: str,
        target: str = "", platform_id: str = "",
    ) -> str:
        """展开模板里的内置模块。

        ``{at}``         → @成员 标签（_render 传了 at= 时不会残留到这里）
        ``{avatar}``     → 成员头像，走 markdown 图片语法，仅 markdown 格式生效
        ``{avatar:160}`` → 指定展示尺寸（夹在 24~320px，自动选清晰度档位）
        旧 ``<@openid>`` → 自动升级为官方新标签

        ``event`` 可以为 None：入群欢迎这类「没有可回复事件、只能发主动消息」
        的场景拿不到 AstrMessageEvent，此时用 ``platform_id`` + ``target``
        （成员 openid）来拼 @ 标签和头像。
        """
        if not text:
            return text
        need_at = "{at}" in text
        need_avatar = "{avatar" in text
        need_legacy = "<@" in text
        if not (need_at or need_avatar or need_legacy):
            return text

        if event is not None:
            try:
                pid = event.get_platform_id()
            except Exception:
                pid = ""
        else:
            pid = platform_id
        appid = self._platform_appid(pid) if pid else ""
        if target:
            openid = str(target).strip()
        else:
            try:
                openid = str(event.get_sender_id() or "").strip() if event is not None else ""
            except Exception:
                openid = ""

        if need_at:
            tag = self._at_tag(openid) if (appid and openid) else ""
            if not tag:  # 非 QQ 官方通道或拿不到 openid：退化成文字 @昵称
                try:
                    nick = str(event.get_sender_name() or "").strip() if event is not None else ""
                except Exception:
                    nick = ""
                tag = f"@{nick}" if nick else ""
            text = text.replace("{at}", tag)

        if need_avatar:

            def _sub(m):
                # 纯文本 / 图片消息里塞不进 markdown 图片，静默去掉避免露出语法
                if not (appid and openid) or fmt != "markdown":
                    return ""
                try:
                    disp = int(m.group(1)) if m.group(1) else 100
                except ValueError:
                    disp = 100
                disp = max(24, min(disp, 320))
                # 小图档（≤40px 展示）只取 40 的源，别为一个表情大小的头像拉 640 大图
                src = next((t for t in self.AVATAR_SRC_TIERS if t >= disp),
                           self.AVATAR_SRC_TIERS[-1])
                url = self.AVATAR_URL_FMT.format(appid=appid, openid=openid, size=src)
                return f"![avatar #{disp}px #{disp}px]({url})"

            text = self._AVATAR_PH_RE.sub(_sub, text)

        if need_legacy and appid:
            # 只升级 QQ 官方通道；频道/其他平台的 <@id> 不是 openid，不能动
            text = self._LEGACY_AT_RE.sub(lambda m: self._at_tag(m.group(1)), text)
        return text

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        """统一回复出口：按模板配置的格式决定怎么发。

        格式（text / markdown / image）由面板「消息模板」页签保存在
        plugin_data/msg_formats.json，内容本身仍在 AstrBot 配置里，
        所以升级不会改变任何既有配置，最坏情况退化成纯文本。
        """
        if not text:
            return
        key = getattr(text, "tpl_key", "") or ""
        fmt = self._tpl_format(key) if key else "text"
        content = self._expand_msg_modules(
            str(text), event, fmt, getattr(text, "tpl_target", "")
        )
        is_img = fmt == "image"
        img_url = ""
        if is_img:
            img_url = str((self._tpl_formats.get(key) or {}).get("url") or "").strip()
        # 图片消息的正文只是配文，允许为空；纯文本 / Markdown 展开后全空就别推空消息
        if not content.strip() and not img_url:
            return
        try:
            if fmt == "markdown":
                try:
                    chain = MessageChain([Plain(content)])
                    chain.use_markdown(True)
                    await event.send(chain)
                    return
                except Exception as e:
                    # markdown 通道失败（40034124 / 内容校验等）→ 退回纯文本，别把消息吞了
                    self.logger.warning(f"markdown 发送失败，降级为纯文本: {e}")
            if is_img:
                if img_url:
                    comps: list = [Image(file=img_url)]
                    if content.strip():
                        comps.append(Plain("\n" + content.strip()))
                    await event.send(MessageChain(comps))
                    return
                # 图片地址为空：退回纯文本，绝不吞消息
            chain = MessageChain([Plain(content)])
            chain.use_markdown(False)  # 显式纯文本，避免被平台默认 markdown 解析
            await event.send(chain)
        except Exception as e:
            self.logger.error(f"回复消息失败: {e}")

    async def _send_group_proactive(
        self, group_openid: str, text: str, platform_id: str, *, at_openid: str = "",
        keyboard: dict | None = None,
    ) -> bool:
        """主动往群里发一条模板消息（入群欢迎这类「没有可回复事件」的场景）。

        为什么必须走主动消息：官方 ``event_id`` 被动回复只支持 3 个事件
        （INTERACTION_CREATE / GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE），
        GROUP_MEMBER_ADD、GROUP_JOIN_REQUEST 都不在白名单里，拿它们的事件 id
        去回会直接 40034027。
        代价是受主动消息频控（未认证 30 条/分钟、单群 20 条/分钟、每群每天
        1000 条），且用户可在 QQ 客户端关掉「允许主动发送」。

        模板格式为图片（image）时只发配文：入群瞬间没有可渲染图片的消息上下文，
        图片模板退化成文字，总比什么都不发好。

        Args:
            keyboard: 内联键盘（按钮）。按钮相关错误码（305007 键盘参数错误、
                40034029 行列超限、40034124 markdown 参数错误、22006 类型不匹配）
                出现时会**去掉键盘重发一次**——宁可少按钮，也别把通知丢了。

        Returns:
            True 表示接口调用成功。
        """
        key = getattr(text, "tpl_key", "") or ""
        fmt = self._tpl_format(key) if key else "text"
        content = self._expand_msg_modules(
            str(text), None, fmt,
            getattr(text, "tpl_target", "") or at_openid,
            platform_id=platform_id,
        )
        if not content.strip():
            self.logger.debug(f"[主动消息] 模板 {key or '?'} 展开后为空，跳过发送")
            return False
        api = self._api_for_group_or_default(group_openid, platform_id)
        if api is None:
            self.logger.warning(
                f"[主动消息] 群 {str(group_openid)[-6:]} 找不到可用 API 客户端，发送跳过"
            )
            return False
        # ---- 发送阶梯：按钮优先，逐级降级，正文最后兜底 ----
        # 为什么不「失败就丢按钮」：markdown 通道对 keyboard 的校验比文本通道严
        # （实测 group_id 含冒号就 40011021 整条拒发），一次失败直接丢按钮，等于
        # 把最值钱的功能丢了。所以按「损失从小到大」逐级试：
        #   ① markdown + 完整键盘（有互斥变灰）
        #   ② markdown + 去掉 group_id 的键盘（按钮还在，只是不互斥）
        #   ③ markdown 纯正文（保住排版）
        #   ④ 纯文本（保住内容）
        plans: list = []
        if keyboard:
            plans.append((True, keyboard))
            if button_kit.has_group_id(keyboard):
                stripped = button_kit.without_group_id(keyboard)
                if stripped:
                    plans.append((True, stripped))
            plans.append((True, None))
            plans.append((False, None))
        elif fmt == "markdown":
            plans.append((True, None))
            plans.append((False, None))
        else:
            plans.append((False, None))

        last_err = None
        for idx, (md, kb) in enumerate(plans):
            kwargs: dict = {"markdown": True} if md else {}
            if kb:
                kwargs["keyboard"] = kb
            try:
                await api.send_group_message(group_openid, content, **kwargs)
                if keyboard and kb is None:
                    self.logger.warning(
                        "[主动消息] 已降级为无按钮消息（正文送达，但群里没有"
                        "「通过/拒绝」按钮）；审批可用 /群管 审批 或面板操作"
                    )
                elif keyboard and kb is not keyboard:
                    self.logger.info(
                        "[主动消息] 去掉 group_id（按钮互斥分组）后重发成功，按钮可用"
                    )
                return True
            except QQApiError as e:
                last_err = e
                code = getattr(e, "code", None)
                if code in self.FATAL_SEND_CODES and keyboard:
                    # 权限 / 频控 / 不在群：换格式一样失败，别为了按钮白刷四档请求。
                    # 注意只在「带按钮」时短路：无按钮的 markdown 降级只有一两档，
                    # 且正文优先，宁可多试一次也别把欢迎语/通知丢了。
                    self.logger.warning(
                        f"[主动消息] 群 {str(group_openid)[-6:]} 发送失败: {e}；"
                        f"{self._explain_send_error(code)}"
                    )
                    return False
                if idx + 1 < len(plans):
                    self.logger.warning(
                        f"[主动消息] 第 {idx + 1} 档发送失败（code={code}），继续降级: {e}"
                    )
        if last_err is not None:
            self.logger.warning(
                f"[主动消息] 群 {str(group_openid)[-6:]} 发送失败（所有降级档位均失败）: "
                f"{last_err}；{self._explain_send_error(getattr(last_err, 'code', None))}"
            )
        return False

    # ------------------------------------------------------------------
    # 群消息入口（指令走标准正则过滤器；普通消息仅做新群欢迎）
    # ------------------------------------------------------------------
    def _extract_text(self, event: AstrMessageEvent) -> str:
        """从消息链提取原始文本。

        注意不能用 event.get_message_str()：AstrBot 的 waking_check 阶段会把以
        唤醒词（配置里曾含 "/"）开头的消息剥离唤醒词，导致 "/禁言" 变成 "禁言"。
        而消息链（get_messages）保留原始文本，必须从这里提取。
        """
        raw_parts = []
        for comp in event.get_messages():
            if isinstance(comp, Plain):
                raw_parts.append(comp.text)
            elif isinstance(comp, At):
                raw_parts.append(f"<@{comp.qq}>")
        text = "".join(raw_parts).strip()
        # 剥离所有 <@openid> 文本（QQ 官方消息中 @ 就是这种纯文本格式）
        return re.sub(r"<@!?[0-9A-Za-z_-]+>", " ", text).strip()

    def _record_recent_msg(self, event: AstrMessageEvent) -> None:
        """缓存当前群消息（原文 + 消息 ID + 时间），供引用撤回按原文匹配。

        只缓存普通消息（message_type != 103）；引用消息（103）自身不缓存，
        避免干扰匹配。每群最多保留 200 条 / 10 分钟内的消息。
        """
        try:
            g = event.get_group_id()
            if not g:
                return
            self._last_active_group = g
            msg_id = str(getattr(event.message_obj, "message_id", "") or "")
            if not msg_id:
                return
            raw = getattr(event.message_obj, "raw_message", None)
            mt = None
            if raw is not None:
                if isinstance(raw, dict):
                    mt = raw.get("message_type")
                else:
                    mt = getattr(raw, "message_type", None)
            try:
                if mt is not None and int(mt) == 103:
                    return  # 引用消息自身不入缓存
            except (TypeError, ValueError):
                pass
            text = self._extract_text(event)
            images = self._extract_images(event)
            if not text and not images:
                return
            lst = self._recent_group_msgs.setdefault(g, [])
            lst.append({
                "id": msg_id,
                "text": text,
                "ts": time.time(),
                "sender": event.get_sender_id() or "",
                "sender_name": event.get_sender_name() or "",
                # 图片地址：仅本地留存，供 /举报 的多模态审核取用（QQ 图片
                # URL 会过期，举报稍晚就可能取不到，所以必须提前存下来）。
                "images": images,
            })
            # 裁剪：最多 200 条，且丢弃超过 10 分钟的旧消息
            now = time.time()
            lst[:] = [r for r in lst if now - r["ts"] <= 600][-200:]
        except Exception:
            pass

    def _match_recent_msg_id(self, group_openid: str, text: str,
                             max_age: float = 150.0) -> str:
        """在消息缓存中按原文查找最近的消息 ID（用于引用撤回）。

        撤回时限 2 分钟，默认只匹配 150 秒内、原文完全一致的最近一条。
        返回消息 ID，未命中返回空串。
        """
        try:
            lst = self._recent_group_msgs.get(group_openid) or []
            now = time.time()
            best = ""
            best_ts = 0.0
            for r in lst:
                if now - r["ts"] > max_age:
                    continue
                if (r.get("text") or "").strip() == (text or "").strip():
                    if r["ts"] >= best_ts:
                        best_ts = r["ts"]
                        best = str(r.get("id") or "")
            return best
        except Exception:
            return ""

    # 旧独立指令 → 新大类 + 子命令。保留映射表，老写法永远不用改。
    _LEGACY_ALIASES = {
        "/解禁": ("/禁言", "解除"),
        "/查禁言": ("/禁言", "名单"),
        "/禁言历史": ("/禁言", "历史"),
        "/通过": ("/审批", "通过"),
        "/拒绝": ("/审批", "拒绝"),
        "/汇报": ("/状态", "日报"),
        "/体检": ("/状态", "群"),
        "/自检": ("/状态", "自检"),
        "/群名": ("/状态", "群名"),
    }
    _LEGACY_NAMES = tuple(k.lstrip("/") for k in _LEGACY_ALIASES)

    def _cmd_alias_of(self, event: AstrMessageEvent, default: str) -> str:
        """指令组别名命中后，还原用户实际敲的是哪个旧名字。

        AstrBot 的 alias 只会把消息路由到同一个 handler，不会告诉我们是哪个别名；
        所以回到消息原文的前两个 token 里找。找不到就用 default。
        """
        text = self._extract_text(event) or ""
        for tok in text.split()[:2]:
            name = tok.lstrip("/")
            if name in self._LEGACY_NAMES:
                return "/" + name
        return default

    def _normalize_aliases(self, cmd: str, parts: list[str]) -> tuple[str, list[str]]:
        """把旧独立指令折成「大类 + 子命令」：/解禁 @x → /禁言 解除 @x。

        这样只有一套处理逻辑，老写法、新写法、面板里改过别名后进来自定义名字，
        最终都会收敛到同一个入口。
        """
        hit = self._LEGACY_ALIASES.get(cmd)
        if not hit:
            return cmd, parts
        new_cmd, sub = hit
        return new_cmd, [new_cmd, sub] + list(parts[1:])

    async def _run_command(
        self, event: AstrMessageEvent, forced_cmd: str | None = None, skip: int = 0
    ) -> None:
        """指令执行入口：提取原始文本 -> 权限校验 -> 分发执行。

        Args:
            forced_cmd: 由 @filter.command 注册的 handler 传入的规范指令名
                （形如 "/禁言"）。面板里改过指令名/别名后，靠它保证内部路由
                到正确的处理逻辑；为 None 时按消息原文解析（兼容入口用）。
            skip: 额外丢弃消息文本前 N 个 token。指令组写法
                （"/群管 禁言 @某人 10分钟"）需要丢掉组名之后、子指令名，
                否则子指令名会被当成参数（比如被误认成"要禁言的人"）。
        """
        try:
            # 防重入：AstrBot 会对所有正则命中的 handler 逐个执行，
            # "/禁言历史" 会被 "/禁言" 与 "/禁言历史" 两个 handler 同时触发，
            # 这里按消息 ID 去重，保证同一条消息只处理一次。
            msg_id = str(getattr(event.message_obj, "message_id", "") or "")
            now = time.time()
            if msg_id and msg_id == self._last_cmd_msg_id and now - self._last_cmd_ts < 5:
                self.logger.debug(f"同一条消息重复触发，已忽略 (msg_id={msg_id})")
                return
            if msg_id:
                self._last_cmd_msg_id = msg_id
                self._last_cmd_ts = now

            self._ensure_poll_task()
            self._ensure_report_task()  # WebUI 刚打开日报开关时，靠这条路径自愈
            self._record_recent_msg(event)  # 引用撤回：原文匹配需要消息缓存
            await self._maybe_welcome_new_group(event)
            text = self._extract_text(event)
            self.logger.debug(f"群消息文本: {text!r}")
            if not text:
                return
            # 指令必须以 "/" 开头，或满足以下两个条件之一才处理：
            # 1) 消息 @ 了机器人（群内常见的不带 / 指令写法）；
            # 2) 首 token 是已知指令名。
            # 这样既兼容 README 所述"不带 / 也可以"，又避免普通聊天内容误触发。
            clean = re.sub(r"<@!?[0-9A-Za-z_-]+>", " ", text).strip()
            if not clean:
                return
            first = clean.split()[0]
            if not first.startswith("/"):
                if not self._is_mentioned_bot(event):
                    return
                if first not in ("禁言", "审批", "状态", "撤回", "群管", "举报", "群规", "审核",
                                 "管理登记", "成员", "帮助", "撤销",
                                 "解禁", "查禁言", "禁言历史", "通过", "拒绝",
                                 "汇报", "体检", "自检", "群名"):
                    return
            await self._handle_command(event, text, forced_cmd=forced_cmd, skip=skip)
        except Exception as e:
            self.logger.error(f"处理群消息异常: {e}")
            import traceback

            self.logger.debug(traceback.format_exc())

    # 按钮点击必须走独立的 button_action handler：AstrBot 唤醒阶段对按钮事件只放行挂了
    # ButtonActionFilter 的 handler，普通 event_message_type 的 handler 会被直接跳过。
    # （踩过的坑：写在 on_group_message 里时，点击只改了客户端按钮状态，服务端一次没执行。）
    if _BUTTON_ACTION_SUPPORTED:

        @_BUTTON_ACTION_DECORATOR()
        async def on_button_action(self, event: AstrMessageEvent):
            """按钮点击：入群审批的「通过 / 拒绝」"""
            await self._consume_button_action(event)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息：成员/消息缓存、巡检、新群欢迎（指令走 @filter.command）"""
        try:
            # 极老版本 AstrBot 可能把按钮事件漏进来，兜一次；正常不会走到这里
            if await self._consume_button_action(event):
                return
            await self._track_group_activity(event)
        except Exception as e:
            self.logger.error(f"处理群消息异常: {e}")
            import traceback

            self.logger.debug(traceback.format_exc())

    async def _consume_button_action(self, event: AstrMessageEvent) -> bool:
        """认领按钮点击并消费事件；返回是否真的处理了。回调路径绝不把异常抛给平台。"""
        try:
            if not await self._handle_button_interaction(event):
                return False
            try:
                event.stop_event()
            except Exception:  # noqa: BLE001 - 老版本没有 stop_event 也无妨
                pass
            return True
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"处理按钮点击异常: {e}")
            import traceback

            self.logger.debug(traceback.format_exc())
            return False

    async def _track_group_activity(self, event: AstrMessageEvent) -> None:
        """群消息的记账动作：昵称缓存 → 成员档案 → 轮询/注入兜底 → 消息缓存 → 巡检 → 新群欢迎。"""
        g = event.get_group_id()
        if g:
            sid = event.get_sender_id()
            sname = event.get_sender_name()
            if sid and sname:
                self._nick_cache.setdefault(g, {})[sid] = sname
            self._record_member_seen(event)
        self._ensure_poll_task()
        self._ensure_member_event_injected()  # 兜底：平台实例就绪后重试注入
        self._record_recent_msg(event)        # 引用撤回：原文匹配需要消息缓存
        await self._maybe_auto_scan(event)    # 审核：仅当「本群」开了巡检开关才会执行
        await self._maybe_welcome_new_group(event)

    # ------------------------------------------------------------------
    # 群成员事件（退群/被移出通知，运行时注入实现，见 member_event.py）
    # 群成员档案（openid <-> 昵称/备注，见 member_registry.py）
    # ------------------------------------------------------------------
    def _members_on(self) -> bool:
        return bool(self._read_config_value(
            ("member_registry", "enabled"), default=True,
            legacy_keys=("member_registry_enabled",),
        ))

    # ------------------------------------------------------------------
    # 生效群范围（basic.target_group_openids）
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_group_target(item) -> tuple[str, str]:
        """把配置项拆成 (platform_id, group_openid)。

        支持三种写法：
          - ``群OpenID``               → platform_id 为空＝不限机器人实例
          - ``机器人实例ID:群OpenID``   → 只在这个平台实例上管这个群
        """
        text = str(item or "").strip()
        if ":" in text:
            head, _, tail = text.partition(":")
            head, tail = head.strip(), tail.strip()
            if head and tail:
                return head, tail
        return "", text

    def _target_groups(self) -> list[str]:
        """生效群范围原始配置项（可能带「机器人实例ID:」前缀）。"""
        raw = self._read_config_value(
            ("basic", "target_group_openids"), default=[], legacy_keys=("target_group_openids",)
        ) or []
        return [str(x).strip() for x in raw if str(x or "").strip()]

    def _target_group_allowed(self, group_openid: str, platform_id: str | None = None) -> bool:
        """群是否在生效范围内（留空＝所有群、所有机器人；兼容带前缀写法）。"""
        targets = self._target_groups()
        if not targets:
            return True
        gid = str(group_openid or "").strip()
        if not gid:
            return False
        pid = str(platform_id or self._known_groups.get(gid) or "").strip()
        for item in targets:
            item_pid, item_gid = self._parse_group_target(item)
            if item_gid.lower() != gid.lower():
                continue
            if not item_pid:
                return True          # 没写机器人前缀＝所有机器人都管
            if not pid or item_pid.lower() == pid.lower():
                return True
        return False

    @staticmethod
    def _explain_send_error(code) -> str:
        """把群消息发送失败的常见错误码翻成一句人话，排障少走弯路。"""
        try:
            c = int(code) if code is not None else 0
        except (TypeError, ValueError):
            c = 0
        table = {
            11251: "11251＝主动消息受限：群内 24 小时内需有人 @ 过机器人，否则主动消息发不出去（可改用被动回复）。",
            11253: "11253＝应用无接口访问权限：该接口是内邀白名单能力，普通机器人本来就用不了，属正常现象。",
            40034027: "40034027＝event_id 不支持该事件类型，只能改发主动消息。",
            40034105: (
                "40034105＝主动消息无权限：该群没有给机器人开「主动消息推送」，"
                "需群主在群设置/添加机器人时的权限项里开启；同一机器人其他群可能正常。"
            ),
            40054005: "40054005＝msg_seq 去重：被动回复复用同一个 event_id 时要自增 msg_seq。",
            40011021: (
                "40011021＝键盘里的 group_id 无效：markdown 消息对按钮互斥分组的校验"
                "比文本消息严，只认短 ASCII 值（插件已改用哈希短 ID，出现此码说明"
                "配置里塞了自定义键盘）。"
            ),
        }
        return table.get(c, f"错误码 {c} 未收录，建议对照平台文档或看 AstrBot / 适配器日志里的原始响应。")

    def _group_in_scope(self, group_openid: str, platform_id: str | None = None) -> bool:
        return self._target_group_allowed(group_openid, platform_id)

    def _record_member_seen(self, event: AstrMessageEvent) -> None:
        """普通群消息：把发言人记进档案。

        官方群消息事件的 author 里只有 member_openid，昵称通常是空的；
        这里只有在适配器真给了昵称时才顺手填（source=msg，人工昵称不会被覆盖）。
        """
        try:
            if not self._members_on():
                return
            if not bool(self._read_config_value(("member_registry", "record_on_message"), default=True)):
                return
            g = event.get_group_id()
            sid = str(event.get_sender_id() or "")
            if not g or not sid or not self._group_in_scope(g, event.get_platform_id()):
                return
            sname = str(event.get_sender_name() or "").strip()
            name = sname if (sname and sname != sid) else ""
            self.members.seen(g, sid, name=name)
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"[成员档案] 记录发言失败: {e}")

    # 入群欢迎去重窗口（秒）：同一事件被重推、或审批通过时已经欢迎过，不再重复发。
    JOIN_WELCOME_DEDUPE_SECONDS = 600.0

    async def _on_group_member_add(self, data: dict, platform_id: str | None = None) -> None:
        """成员入群（GROUP_MEMBER_ADD）：建档案 + 发欢迎语。

        事件体只有 timestamp / group_openid / member_openid（官方文档如此，
        没有昵称、没有 user_openid、外层虽带事件 id 但该事件不在被动回复
        白名单里），所以欢迎语只能走主动消息。
        """
        g = str(data.get("group_openid") or "")
        member = str(data.get("member_openid") or "")
        if not g or not member:
            self.logger.warning(f"[成员事件] 入群事件缺少必要字段: {data}")
            return
        if not self._group_in_scope(g, platform_id):
            return

        # 1) 档案：入群瞬间就能建（只有 openid），退群时「在群多久」才有据可查
        if self._members_on():
            try:
                rec = self.members.seen(
                    g, member,
                    ts=data.get("timestamp") or None,
                    count_msg=False,
                    user_openid=str(data.get("user_openid") or ""),
                )
                self.logger.info(
                    f"[成员档案] 群 {g} 成员入群: {member_registry_lib.short_id(member)}"
                    f"（{'已在档' if rec and rec.get('first_seen') else '新建'}）"
                )
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"[成员档案] 入群建档失败: {e}")

        # 2) 欢迎语：不受「成员档案」开关影响，单独由 member_event 配置控制
        try:
            await self._welcome_member_on_join(g, member, platform_id)
        except Exception as e:  # noqa: BLE001
            self.logger.exception(f"[入群欢迎] 处理异常: {e}")

    async def _welcome_member_on_join(
        self, group_openid: str, member_openid: str, platform_id: str | None
    ) -> None:
        """成员真进群时发欢迎语（主动消息，见 _send_group_proactive）。"""
        if not bool(self._read_config_value(
            ("member_event", "enable_member_add_welcome"), default=True,
            legacy_keys=("enable_member_add_welcome",),
        )):
            self.logger.debug("[入群欢迎] 配置已关闭，跳过")
            return

        now = time.time()
        # 先抢占槽位再发：审批通过路径和入群事件路径是两条独立的并发流程，
        # 谁先到谁发，"检查→发送→写记录"的旧写法在两者交错时必然双发。
        if not self._claim_join_welcome(group_openid, member_openid):
            last = float(
                self._join_welcomed.get(f"{group_openid}:{member_openid}") or 0
            )
            self.logger.info(
                f"[入群欢迎] 群 {str(group_openid)[-6:]} 成员 "
                f"{member_registry_lib.short_id(member_openid)} "
                f"{int(now - last)} 秒前刚欢迎过（事件重推或审批已欢迎），跳过"
            )
            return

        # 名字：进群瞬间档案通常只有 openid，能用就用，实在没有给个中性称呼
        username = ""
        if self._members_on():
            try:
                username = str(
                    (self.members.get(group_openid, member_openid) or {}).get("nickname") or ""
                ).strip()
            except Exception:  # noqa: BLE001
                username = ""

        try:
            group_name = self._welcome_group_name(group_openid)
        except Exception:  # noqa: BLE001
            group_name = group_openid

        text = self._render(
            "msg_welcome_after_approve",
            username=username or "新朋友",
            group_name=group_name,
            bot_name=platform_id or "",
            at=member_openid,
        )
        if not str(text or "").strip():
            self.logger.info("[入群欢迎] 欢迎语模板为空，跳过")
            self._release_join_welcome(group_openid, member_openid)
            return

        ok = await self._send_group_proactive(
            group_openid, text, platform_id or "", at_openid=member_openid
        )
        if ok:
            self.logger.info(
                f"[入群欢迎] 群 {str(group_openid)[-6:]} 已向 "
                f"{member_registry_lib.short_id(member_openid)} 发送欢迎语"
            )
        else:
            # 发失败就释放槽位，下次事件重推还能再试一次
            self._release_join_welcome(group_openid, member_openid)
            self.logger.warning(
                f"[入群欢迎] 群 {str(group_openid)[-6:]} 欢迎语发送失败："
                f"主动消息需要该群允许机器人推送（部分群需群主在权限项里开启），"
                f"也可能撞上频控或用户在客户端关掉了「允许主动发送」"
            )

    def _claim_join_welcome(self, group_openid: str, member_openid: str) -> bool:
        """抢占该成员本次入群的欢迎发送权（先写表、后发送）。

        审批通过（按钮/指令/轮询）和 GROUP_MEMBER_ADD 是两条独立流程，
        而且事件往往比审批流程的后续代码先跑完，所以"发送前检查、发送后
        记录"必然漏掉并发窗口。改成发送前先把时间戳写进表里：谁先占到谁发，
        另一条路径直接跳过。

        Returns:
            True 表示抢到（由本次调用负责发欢迎），False 表示窗口内已有人欢迎过。
        """
        now = time.time()
        key = f"{group_openid}:{member_openid}"
        last = float(self._join_welcomed.get(key) or 0)
        if last and now - last < self.JOIN_WELCOME_DEDUPE_SECONDS:
            return False
        self._join_welcomed[key] = now
        self._prune_join_welcomed(now)
        return True

    def _release_join_welcome(self, group_openid: str, member_openid: str) -> None:
        """承认领失败／占位作废时把槽位还回去。"""
        self._join_welcomed.pop(f"{group_openid}:{member_openid}", None)

    def _welcome_group_name(self, group_openid: str) -> str:
        """欢迎语里用的群名：优先真实群名，取不到才退回 openid。"""
        try:
            name = str(self.mod.get_group_meta(group_openid).get("name") or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
        return name or group_openid

    def _prune_join_welcomed(self, now: float | None = None) -> None:
        """清理入群欢迎去重表：过期记录删掉，最多留 500 条。"""
        now = float(now if now is not None else time.time())
        keep = {
            k: v for k, v in self._join_welcomed.items()
            if now - float(v or 0) < self.JOIN_WELCOME_DEDUPE_SECONDS
        }
        if len(keep) > 500:
            for k, _ in sorted(keep.items(), key=lambda kv: kv[1])[: len(keep) - 500]:
                keep.pop(k, None)
        self._join_welcomed = keep

    async def _on_group_member_remove(self, data: dict, platform_id: str | None = None) -> None:
        """成员退群/被移出：先查档案，再按配置在群内发言通知。

        Args:
            data: 网关事件体（含 group_openid / member_openid / user_openid，
                以及 member_event.py 补进来的 _event_id）。
            platform_id: 事件来源的 qq_official 实例 ID（member_event.py 注入
                时通过闭包传入），用于多实例下精确路由 API 客户端。
        """
        try:
            g = str(data.get("group_openid") or "")
            member = str(data.get("member_openid") or "")
            if not g or not member:
                self.logger.warning(f"[成员事件] 退群事件缺少必要字段: {data}")
                return
            if not self._group_in_scope(g, platform_id):
                return

            # 1) 先查档案（此时 status 还是 active，能算在群时长）
            rec = self.members.get(g, member)
            joined_at = float((rec or {}).get("first_seen") or 0)
            now = float(data.get("timestamp") or time.time())
            msgs = int((rec or {}).get("msg_count") or 0)
            username = str((rec or {}).get("nickname") or "").strip()
            name_src = "existing" if username else ""
            user_openid = str(data.get("user_openid") or "")

            # 2a) 跨群认人：事件里的 user_openid 在同一机器人下跨群稳定，
            #     别的群补过名字就直接借过来（source 记为 cross）。
            if not username and user_openid:
                got, from_group = self.members.name_by_user_openid(user_openid, exclude_group=g)
                if got:
                    username = got
                    name_src = "cross"
                    self.logger.info(
                        f"[成员档案] 群 {g} 成员 {member_registry_lib.short_id(member)}"
                        f" 借用「{from_group[-6:]}」群的名字: {got}"
                    )

            # 2b) 还没有名字，再试一次接口（白名单机器人可用，普通机器人报 11253）
            if not username and bool(self._read_config_value(
                ("member_event", "member_remove_try_fetch_name"), default=True,
                legacy_keys=("member_remove_try_fetch_name",),
            )):
                fetched = await self._resolve_member_name(g, member)
                if fetched:
                    username = fetched
                    name_src = "api"

            # 3) 落档：有档案标退群，没档案也建一条「只有 openid」的记录便于追溯
            if self._members_on():
                try:
                    if rec is None:
                        rec = self.members.create_left(g, member, ts=now)
                        if user_openid:
                            self.members.bind_user_openid(g, member, user_openid)
                    else:
                        self.members.mark_left(g, member, ts=now)
                        if user_openid:
                            self.members.bind_user_openid(g, member, user_openid)
                except Exception as e:  # noqa: BLE001
                    self.logger.debug(f"[成员档案] 标记退群失败: {e}")
                # 刚补到的名字（跨群借的 / 接口查的）写回本群档案
                if username and name_src in ("cross", "api"):
                    try:
                        cur = self.members.get(g, member) or {}
                        if not str(cur.get("nickname") or "").strip():
                            self.members.set_name(g, member, username, source=name_src)
                    except Exception as e:  # noqa: BLE001
                        self.logger.debug(f"[成员档案] 回写昵称失败: {e}")

            if not bool(self._read_config_value(
                ("member_event", "enable_member_remove_notify"),
                default=False,
                legacy_keys=("enable_member_remove_notify",),
            )):
                return

            # 4) 组通知文案
            days = int(max(0, now - joined_at) // 86400) if joined_at else 0
            extra = ""
            if joined_at:
                extra = f"\n在群 {days} 天 · 共发言 {msgs} 条"
            text = self._render(
                "msg_member_remove_notify",
                username=username or "某位成员",
                short=member_registry_lib.short_id(member),
                openid=member,
                at=member,
                days=days,
                msgs=msgs,
                extra=extra,
                group=(self.mod.get_group_meta(g).get("name") or g),
            )
            if not str(text or "").strip():
                return

            api = self._api_for_group_or_default(g, fallback_pid=platform_id)
            if api is None:
                self.logger.warning(f"[成员事件] 群 {g} 无可用 API 客户端，无法发送退群通知")
                return

            # 5) 发送。官方「支持 event_id 被动回复」的事件只有
            #    INTERACTION_CREATE / GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE，
            #    GROUP_MEMBER_REMOVE 不在名单里（会返回 40034027），
            #    所以默认直接发主动消息；想试被动回复的用户可开 passive_first，
            #    失败会自动降级，不会把通知丢掉。
            mode = str(self._read_config_value(
                ("member_event", "member_remove_notify_mode"), default="active"
            ) or "active")
            event_id = str(data.get("_event_id") or "")
            sent = False
            if mode == "passive_first" and event_id:
                try:
                    await api.send_group_message(g, text, event_id=event_id)
                    sent = True
                    self.logger.info(f"[成员事件] 群 {g} 退群通知已用 event_id 被动回复")
                except Exception as e:  # noqa: BLE001
                    code = getattr(e, "code", None)
                    self.logger.info(
                        f"[成员事件] 被动回复失败(code={code})，降级为主动消息: {e}"
                    )
            if not sent:
                try:
                    await api.send_group_message(g, text)
                    self._member_perm_blocked.discard(g)
                    self.logger.info(
                        f"[成员事件] 群 {g} 成员 {member_registry_lib.short_id(member)}"
                        f"({username or '无名'}) 退群，已发送主动消息通知"
                    )
                except Exception as e:  # noqa: BLE001
                    code = getattr(e, "code", None)
                    hint = self._explain_send_error(code)
                    try:
                        if int(code) == 40034105:
                            self._member_perm_blocked.add(g)
                    except (TypeError, ValueError):
                        pass
                    self.logger.error(
                        f"[成员事件] 退群通知发送失败(code={code if code is not None else '未知'}): {e}；{hint}"
                    )
        except Exception as e:  # noqa: BLE001
            self.logger.exception(f"[成员事件] 处理退群事件异常: {e}")

    async def _resolve_member_name(self, group_openid: str, member_openid: str) -> str:
        """尽力获取成员昵称：档案 -> 内存缓存 -> 群成员接口。查不到返回空串。"""
        try:
            rec = self.members.get(group_openid, member_openid) or {}
            if str(rec.get("nickname") or "").strip():
                return str(rec["nickname"]).strip()
        except Exception:  # noqa: BLE001
            pass
        cached = (self._nick_cache.get(group_openid) or {}).get(member_openid)
        if cached:
            return cached
        api = self._api_for_group_or_default(group_openid)
        if api is not None:
            try:
                info = await api.get_group_member(group_openid, member_openid)
                name = (info or {}).get("nickname") or (info or {}).get("username") or ""
                if name:
                    self._nick_cache.setdefault(group_openid, {})[member_openid] = name
                    # 接口给了名字就顺手回填档案（人工昵称不会被覆盖）
                    if self._members_on():
                        try:
                            rec = self.members.get(group_openid, member_openid)
                            if rec is not None and rec.get("name_source") != "manual":
                                self.members.set_name(group_openid, member_openid, name, source="api")
                        except Exception:  # noqa: BLE001
                            pass
                    return name
            except Exception:
                pass
        return ""

    def _api_for_group_or_default(
        self, group_openid: str, fallback_pid: str | None = None
    ) -> Optional[QQApi]:
        """优先按群映射取 API；取不到时用事件来源实例（fallback_pid）。

        注意：不能硬编码 "qq_official" —— 那是平台名不是实例 ID，
        get_platform_inst() 按实例 ID 匹配，多实例配置下必然失败。
        """
        pid = self._known_groups.get(group_openid)
        if not pid and fallback_pid:
            pid = fallback_pid
        if pid:
            try:
                return self._get_api(pid)
            except QQApiError as e:
                self.logger.debug(f"按 {pid} 取 API 失败: {e}")
        return None

    # ------------------------------------------------------------------
    # 标准指令：用 @filter.command 注册，会出现在
    # 「插件 → 管理行为 → 指令」列表里（可搜索、可单独禁用、可改别名/权限）。
    # 执行仍然统一走 _run_command，权限与配置开关逻辑和以前完全一致；
    # forced_cmd 把本 handler 对应的规范指令名传给分发器，这样即使面板里
    # 改了指令名/加了别名，内部也能正确路由。
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 指令布局：所有指令统一收进指令组「群管」，WebUI「管理行为 → 指令」里
    # 只有一条「群管」，展开是 14 个子指令 —— 面板清爽，也不会有重复条目。
    # 写法：/群管 禁言 @用户 10分钟、/群管 撤回 …；单独发 /群管 自动列出指令树。
    # 全部子指令共用 _run_command（skip=1 丢掉组名与子指令名），权限逻辑不变。
    # ------------------------------------------------------------------
    @filter.command_group("群管")
    def qgm_group(self):
        """群管指令组：禁言 / 撤回 / 审批 / 群规审核 / 词库相关操作"""

    @qgm_group.command("帮助")
    async def g_help(self, event: AstrMessageEvent):
        """查看本插件完整帮助（比自动指令树更详细）"""
        await self._run_command(event, forced_cmd="/群管", skip=1)

    @qgm_group.command("禁言", alias={"解禁", "查禁言", "禁言历史"})
    async def g_mute(self, event: AstrMessageEvent):
        """禁言 @用户 时长 [理由]｜禁言 解除 @用户｜禁言 名单｜禁言 历史"""
        await self._run_command(event, forced_cmd=self._cmd_alias_of(event, "/禁言"), skip=1)

    @qgm_group.command("撤回")
    async def g_recall(self, event: AstrMessageEvent):
        """撤回消息（先引用目标消息再发）"""
        await self._run_command(event, forced_cmd="/撤回", skip=1)

    @qgm_group.command("审批", alias={"通过", "拒绝"})
    async def g_list_requests(self, event: AstrMessageEvent):
        """待处理入群申请｜审批 通过 N｜审批 拒绝 N [理由]"""
        await self._run_command(event, forced_cmd=self._cmd_alias_of(event, "/审批"), skip=1)

    @qgm_group.command("撤销")
    async def g_revoke(self, event: AstrMessageEvent):
        """撤销一条处罚记录并致歉：/群管 撤销 <记录ID> [理由]"""
        await self._run_command(event, forced_cmd="/撤销", skip=1)

    @qgm_group.command("状态", alias={"汇报", "体检", "自检", "群名"})
    async def g_status(self, event: AstrMessageEvent):
        """日报（默认）｜状态 群｜状态 自检｜状态 群名"""
        await self._run_command(event, forced_cmd=self._cmd_alias_of(event, "/状态"), skip=1)

    @qgm_group.command("审核")
    async def g_mod(self, event: AstrMessageEvent):
        """群规审核（状态 / 试运行 / 巡检 / 测试）"""
        await self._run_command(event, forced_cmd="/审核", skip=1)

    @qgm_group.command("管理登记")
    async def g_admin_register(self, event: AstrMessageEvent):
        """审核提醒名单（列表 / 取消）"""
        await self._run_command(event, forced_cmd="/管理登记", skip=1)

    @qgm_group.command("举报")
    async def g_report(self, event: AstrMessageEvent):
        """引用违规消息后举报（所有人可用）"""
        await self._run_command(event, forced_cmd="/举报", skip=1)

    @qgm_group.command("群规")
    async def g_rules(self, event: AstrMessageEvent):
        """查看 / 配置本群群规"""
        await self._run_command(event, forced_cmd="/群规", skip=1)

    @qgm_group.command("成员")
    async def g_members(self, event: AstrMessageEvent):
        """群成员档案（列表 / 改名 / 备注 / 删除 / 拉取昵称）"""
        await self._run_command(event, forced_cmd="/成员", skip=1)

    def _ensure_member_event_injected(self) -> None:
        """确保群成员事件注入完成；失败时延迟重试。

        解决两个问题：
        1. 插件 initialize 时平台实例可能尚未就绪（AstrBot 启动顺序），
           此时注入会失败，等第一条群消息进来时平台必然已就绪，可重试；
        2. 多实例配置下 get_platform_inst("qq_official") 按实例 ID 匹配
           必然返回 None（"qq_official" 只是平台名），必须遍历平台实例注入。
        重试间隔 300 秒，避免反复触发 websocket 重连。
        """
        if self._member_event_ok:
            return
        now = time.time()
        if now - self._last_inject_attempt < 300:
            return
        self._last_inject_attempt = now
        try:
            n = member_event.install_member_event_support(self, self.context)
            if n > 0:
                self._member_event_ok = True
                self.logger.info(f"群成员事件注入成功（{n} 个 qq_official 实例）")
        except Exception as e:
            self.logger.warning(f"群成员事件注入失败（不影响其他功能）: {e}")

    async def initialize(self) -> None:
        """插件激活时立即启动轮询任务。

        注意：不能依赖群消息触发（_run_command 里的 _ensure_poll_task），
        否则群里没人发消息时轮询不启动，入群申请通知永远不会推送。
        """
        self._refresh_timezone()
        self._ensure_poll_task()
        self._ensure_report_task()
        self._ensure_member_event_injected()
        # 1.4.0：把「申请从哪来」和「按钮能不能用」在启动日志里讲清楚，
        # 排障时第一眼就能看出是配置问题还是平台问题。
        mode = self._join_event_mode()
        buttons_on, perm, limit = self._join_button_options()
        self.logger.info(
            f"[审批] 申请获取方式={mode}"
            f"（{'事件为主+兜底轮询' if mode == 'event_first' else '纯轮询' if mode == 'interval' else '仅事件'}），"
            f"兜底间隔={self._poll_interval_seconds()}s"
        )
        if buttons_on and not self._button_channel_ready():
            self.logger.warning(
                "[审批] 已开启「申请通知带审批按钮」，但当前 AstrBot 不满足按钮通道要求"
                "（需要 AstrMessageEvent.get_button_interaction + filter.button_action，"
                "后者缺失时点击事件会在唤醒阶段被丢弃），按钮自动禁用，退回纯文本通知"
            )
        elif buttons_on:
            self.logger.info(
                f"[审批] 审批按钮已启用：权限={perm}（{'仅群主/管理员' if perm == 'admin' else '所有人可点，插件复核'}），"
                f"每条通知最多 {limit} 个申请"
            )
        # 变体索引跟着词库走：这里同步建一次（词库没变时只是签名比对，很便宜），
        # 避免第一条举报进来时才现建、把审核延迟拉长。
        try:
            t0 = time.perf_counter()
            self._variant_sync()
            st = self.variant.stats()
            self.logger.info(
                f"[变体] 检测索引就绪：拼音 {st['pinyin_chars']} 字 / 形近 {st['confusable_groups']} 组 / "
                f"词条 {st['indexed_words']}（{(time.perf_counter() - t0) * 1000:.0f}ms）"
            )
        except Exception as e:
            self.logger.warning(f"[变体] 索引构建失败，变体检测已降级关闭：{e}")

    def _refresh_timezone(self) -> str:
        """把 AstrBot 全局配置的 timezone 同步给时间渲染函数（面板与消息共用）。"""
        name = ""
        try:
            cfg = self.context.get_config()
            if cfg is not None:
                name = cfg.get("timezone") if hasattr(cfg, "get") else ""
        except Exception:
            name = ""
        return set_active_timezone(str(name or "Asia/Shanghai"))

    async def terminate(self) -> None:
        """插件禁用/重载时取消轮询任务，避免旧实例任务残留（会与
        新实例的轮询并发，导致重复请求甚至重复通知）。"""
        task = self._poll_task
        self._poll_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # 定时汇报任务同理：不取消的话重载后会和新实例一起发，一天变两条
        rtask = self._report_task
        self._report_task = None
        if rtask and not rtask.done():
            rtask.cancel()
            try:
                await rtask
            except (asyncio.CancelledError, Exception):
                pass

        # 入群申请的「合并推送」任务也要收掉：它 sleep(2s) 后才真正发消息，
        # 插件重载时旧实例的任务若还活着，会和（可能已经重发一遍的）新实例
        # 各推一条，群里就重复了。取消掉最稳，漏掉的那批下次事件会重新触发。
        for jtask in list(self._join_notify_tasks.values()):
            if jtask and not jtask.done():
                jtask.cancel()
        self._join_notify_tasks.clear()

        # 成员档案攒在内存里（发言计数 60s 才落一次盘），退出前补一次
        try:
            self.members.save()
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"保存成员档案失败（忽略）: {e}")

        # 释放每个 QQApi 持有的 aiohttp.ClientSession。
        # 不做的话，每次重载/禁用插件都会泄漏一个连接池与若干 socket，
        # 反复重载会累积到文件描述符耗尽（aiohttp 退出时还会刷 Unclosed client session）。
        for api in list(self._apis.values()):
            try:
                await api.close()
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"关闭 HTTP 会话失败（忽略）: {e}")
        self._apis.clear()

        # 日志是攒批落盘的，退出前把队列刷干净并停掉后台线程
        try:
            self.logger.close()
        except Exception:
            pass

    def _ensure_poll_task(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
            self.logger.debug("已启动入群申请轮询任务")

    # ------------------------------------------------------------------
    # 定时汇报（群管日报）
    # ------------------------------------------------------------------
    # 为什么是「后台任务 + 每分钟对点」而不是 cron：
    #   插件里没有调度器依赖，AstrBot 也不给插件注册 cron 的稳定接口；
    #   自建一个 30 秒 tick 的 asyncio 任务最简单，且能在插件重载后自愈。
    # 为什么要「过了时间点就补发」而不是「整点才发」：
    #   AstrBot 重启/插件热重载很容易错过整分钟，严格对点会整天不发。
    #   代价是同一分钟内可能晚几分钟发，但一天只有一条，可以接受。
    def _report_options(self) -> tuple[bool, tuple[int, int], str]:
        """(是否开启, (时, 分), 目标群 openid)。配置非法时逐项兜底，不抛异常。"""
        enabled = bool(self._read_config_value(
            ("approval", "report_enabled"), default=False, legacy_keys=("report_enabled",),
        ))
        raw_time = str(self._read_config_value(
            ("approval", "report_time"), default="09:00", legacy_keys=("report_time",),
        ) or "").strip()
        hh, mm = 9, 0
        m = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", raw_time)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                hh, mm = h, mi
            else:
                self.logger.warning(f"[日报] 汇报时间 {raw_time!r} 超出范围，按 09:00 处理")
        elif raw_time:
            self.logger.warning(f"[日报] 汇报时间 {raw_time!r} 格式不对（要 HH:MM），按 09:00 处理")
        target = str(self._read_config_value(
            ("approval", "report_group"), default="", legacy_keys=("report_group",),
        ) or "").strip()
        return enabled, (hh, mm), target

    def _match_group(self, token: str) -> tuple[str, str] | None:
        """把一个「群 openid / 群备注名 / 群名」解析成 (group_openid, platform_id)。"""
        pid = self._known_groups.get(token)
        if pid:
            return token, pid
        # 填的不是 openid 时，按「备注名 / 群名」再认一次：
        # WebUI 里翻 openid 太反人类，管理员多半直接写群叫什么。
        for g in self._known_groups:
            try:
                meta = self.mod.get_group_meta(g) or {}
                names = {
                    str(meta.get("alias") or "").strip(),
                    str(meta.get("name") or "").strip(),
                    str(self.mod.display_name(g) or "").strip(),
                }
            except Exception:  # noqa: BLE001
                names = set()
            if token in {n for n in names if n}:
                self.logger.info(f"[日报] 汇报群按名称匹配到 {token} → {g[-6:]}")
                return g, self._known_groups[g]
        return None

    def _report_candidates(self, target: str) -> list[tuple[str, str]]:
        """定时汇报要发到哪些群（按群逐个汇报，不再只挑一个群）。

        - 留空（默认）＝**所有已知群**，各自群里有待处理申请就发到各自群；
        - 填了＝只汇报匹配到的群，支持逗号 / 空格分隔多个 openid 或群名，
          适合「只想让主群收日报」的场景。
        """
        if not target:
            return list(self._known_groups.items())
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for token in re.split(r"[,，;；\s]+", target):
            token = token.strip()
            if token:
                hit = self._match_group(token)
                if hit is None:
                    self.logger.warning(
                        f"[日报] 汇报群 {token!r} 既不是已知 openid 也匹配不到群名，已跳过"
                    )
                    continue
                if hit[0] not in seen:
                    seen.add(hit[0])
                    out.append(hit)
        return out

    def _ensure_report_task(self) -> None:
        """确保日报任务在跑。WebUI 里刚打开开关时也能立刻生效（不用重载插件）。"""
        if not self._report_options()[0]:
            return
        if self._report_task is None or self._report_task.done():
            self._report_task = asyncio.create_task(self._report_loop())
            enabled, (hh, mm), target = self._report_options()
            self.logger.info(
                f"[日报] 定时汇报任务启动：每天 {hh:02d}:{mm:02d}（时区 {active_timezone_name()}），"
                f"汇报范围={target or '所有已知群（按群逐个汇报）'}"
            )

    async def _report_loop(self) -> None:
        while True:
            try:
                await self._maybe_send_report()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"[日报] 汇报任务异常: {e}")
            await asyncio.sleep(30)

    def _local_now(self):
        tz = _active_tz()
        return datetime.now(tz) if tz else datetime.now()

    async def _maybe_send_report(self) -> None:
        """每 30 秒被叫一次：到点后把「有未处理申请的群」逐个汇报一遍，每天一轮。

        为什么按群发而不是发到一个汇总群：待处理申请是**各群自己的事**，
        汇报发到群里是给该群管理员看的（谁申请进这个群、这个群还压着几条），
        汇总到一个群既没意义也容易泄别的群的信息。
        """
        enabled, (hh, mm), target = self._report_options()
        if not enabled:
            return
        now = self._local_now()
        today = now.strftime("%Y-%m-%d")
        if self._report_last_day == today:
            return
        if (now.hour, now.minute) < (hh, mm):
            return
        groups = self._report_candidates(target)
        if not groups:
            self.logger.warning("[日报] 还没有任何已知群，今天跳过定时汇报")
            self._report_last_day = today  # 避免每 30 秒刷一次日志
            self._save_report_state()
            return

        sent = skipped = failed = 0
        for group_openid, platform_id in groups:
            try:
                pending = await self._refresh_requests(group_openid, platform_id)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    f"[日报] 拉取群 {group_openid[-6:]} 待处理申请失败，改用缓存: {e}"
                )
                pending = self._pending_list(group_openid)
            # 拉取失败时缓存可能是旧的，宁可这轮不发，也别推一条「假日报」
            err = self._join_error_code.get(group_openid)
            if err:
                self.logger.warning(
                    f"[日报] 群 {group_openid[-6:]} 申请列表不可用（code={err}），本轮跳过"
                )
                skipped += 1
                continue
            if not pending:
                skipped += 1
                continue
            text = await self._build_report_text(group_openid, platform_id, pending=pending)
            ok = await self._send_group_proactive(group_openid, text, platform_id)
            if ok:
                sent += 1
                self.logger.info(
                    f"[日报] 已发送到群 {group_openid[-6:]}"
                    f"（待处理 {len(pending)} 条，{now.strftime('%H:%M')}）"
                )
            else:
                failed += 1
                self.logger.warning(
                    f"[日报] 群 {group_openid[-6:]} 发送失败；"
                    "主动消息可能受权限/频控限制，可用 /群管 汇报 手动查看"
                )
        self._report_last_day = today
        self._save_report_state()
        self.logger.info(
            f"[日报] 本轮完成：发送 {sent} 群 / 无待处理跳过 {skipped} 群 / 失败 {failed} 群"
        )

    async def _build_report_text(
        self, group_openid: str, platform_id: str, *, pending: list[dict] | None = None,
    ) -> str:
        """拼日报正文：待处理申请（含名单）+ 当前禁言人数。

        Args:
            pending: 已经拉好的待处理列表（定时汇报会先拉一次用于判断「要不要发」），
                不传则内部自己拉。
        """
        now = self._local_now()
        if pending is None:
            try:
                pending = await self._refresh_requests(group_openid, platform_id)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"[日报] 拉取待处理申请失败，改用缓存: {e}")
                pending = self._pending_list(group_openid)
        if pending:
            limit = 10
            lines = [self._fmt_request_line(i + 1, r) for i, r in enumerate(pending[:limit])]
            if len(pending) > limit:
                lines.append(f"…另有 {len(pending) - limit} 条，发 /群管 审批 全部 查看")
            text_list = "\n".join(lines)
        else:
            text_list = "暂无待处理申请 ✅"

        muted = 0
        try:
            data = await self._get_api(platform_id).get_mute_state(group_openid)
            muted = len(data.get("members") or [])
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"[日报] 查询禁言人数失败（按 0 计）: {e}")

        meta = self.mod.get_group_meta(group_openid)
        return str(self._render(
            "msg_report_daily",
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H:%M"),
            pending_count=len(pending),
            list=text_list,
            muted_count=muted,
            group=str(meta.get("name") or "") or group_openid[-6:],
        ))

    # ------------------------------------------------------------------
    # 新群欢迎（机器人进入新群首次交互）
    # ------------------------------------------------------------------
    async def _maybe_welcome_new_group(self, event: AstrMessageEvent) -> None:
        """兜底方案：机器人进新群后的欢迎。

        优先由 GROUP_ADD_ROBOT 事件在入群瞬间发（被动消息）；这里只在
        「事件没收到（重启丢事件 / 插件装到已有群）」或「事件里发送失败」时补一次。
        事件成功的群会在 _intro_done 里标记，本逻辑自动撤销、不再重复欢迎。
        """
        g = event.get_group_id()
        if not g:
            return
        pid = event.get_platform_id()
        first_time = g not in self._known_groups
        if first_time:
            self._known_groups[g] = pid
            self._save_known_groups()
        if not self._initialized:
            self._initialized = True
            if first_time:
                self.logger.info(f"首轮初始化: 记录群 {g}（本次不发送欢迎）")
            return
        if g in self._intro_done:
            if first_time:
                self.logger.info(
                    f"群 {g} 的自我介绍已由「{self._intro_done[g]}」发出，跳过首条消息欢迎"
                )
            return
        # 只有「刚发现的群」或「收到过入群事件但没发成功」的群才走兜底
        if (not first_time) and g not in self._intro_pending:
            return
        self.logger.info(f"新群 {g}（platform={pid}）未收到/未发出入群自我介绍，走兜底欢迎")
        if not bool(self._read_config_value(("basic", "enable_new_group_welcome"), default=True, legacy_keys=("enable_new_group_welcome",))):
            # 兜底开关也关着 -> 本群自我介绍环节就此了结，清掉挂起标记
            self._intro_pending.pop(g, None)
            self._save_intro_state()
            return
        group_name = (self.mod.get_group_meta(g).get("name") or g)
        text = self._render("msg_welcome_new_group", bot_name=pid, group_name=group_name)
        if not str(text or "").strip():
            # 兜底话术留空＝复用自我介绍（同一场景不要再出现两套说法）
            text = self._render("msg_group_intro", bot_name=pid, group_name=group_name)
        if not str(text or "").strip():
            self.logger.info(f"群 {g} 的兜底话术与自我介绍文案都为空，跳过兜底欢迎")
            self._intro_pending.pop(g, None)
            self._save_intro_state()
            return
        self.logger.info(f"向新群发送欢迎: {text[:80]}")
        await self._reply(event, text)
        self._mark_intro_done(g, "message")

    # ------------------------------------------------------------------
    # 指令分发
    # ------------------------------------------------------------------
    async def _handle_command(
        self,
        event: AstrMessageEvent,
        text: str,
        forced_cmd: str | None = None,
        skip: int = 0,
    ) -> None:
        # QQ 官方消息中 @ 以 <@openid> 文本形式保留在 content 里（AstrBot 适配器
        # 只会清理机器人自身的 @）。这里统一剥离所有 <@...> / <@!...> 片段（替换
        # 为空格）再解析参数，免得 @ 文本混进理由之类的字段；目标成员不依赖这段
        # 文本，而是从 raw_message.mentions 读取。
        text = re.sub(r"<@!?[0-9A-Za-z_-]+>", " ", text).strip()
        parts = text.split()
        if forced_cmd:
            # 注册指令进入：指令名以注册时登记的为准，
            # 既兼容面板重命名/别名，也保证 parts[0] 与旧行为一致。
            cmd = forced_cmd
            # skip=1 用于指令组：丢掉组名后面紧跟的子指令名（"/群管 禁言" 的 "禁言"）
            rest = parts[1 + skip:] if parts else []
            parts = [forced_cmd] + rest
        else:
            if not parts:
                return
            cmd = parts[0]
            if not cmd.startswith("/"):
                # 兼容不带 / 的写法（"禁言" 与 "/禁言" 等价）
                cmd = "/" + cmd

        cmd, parts = self._normalize_aliases(cmd, parts)

        if cmd == "/群管":
            await self._reply(event, self._render("msg_help", bot_name=event.get_platform_id()))
            return

        # /举报：所有群成员都能用（举报的意义就在于此），不受管理员门槛限制
        if cmd == "/举报":
            await self._cmd_report_violation(event, parts)
            return

        # /群规：查看对所有人生效；增删改在 _cmd_rules 内部单独校验管理员
        if cmd == "/群规":
            await self._cmd_rules(event, parts)
            return

        if cmd not in ADMIN_COMMANDS:
            return  # 非本插件指令，不打扰

        # 群白名单检查
        if not self._target_group_allowed(event.get_group_id(), event.get_platform_id()):
            self.logger.debug(f"群 {event.get_group_id()} 不在管理名单，忽略")
            return

        # 管理员检查
        if not self._is_admin(event):
            self.logger.info(
                f"权限不足: sender={event.get_sender_id()} 尝试执行 {cmd}"
            )
            await self._reply(
                event,
                self._render(
                    "msg_not_admin",
                    username=event.get_sender_name() or event.get_sender_id(),
                ),
            )
            event.call_llm = True  # 阻止 LLM 介入
            return

        event.call_llm = True  # 阻止 LLM 介入（已处理）
        if cmd == "/审核":
            await self._cmd_mod(event, parts)
            return
        if cmd == "/禁言":
            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub in ("解除", "解禁", "unmute"):
                await self._cmd_unmute(event, ["/解禁"] + parts[2:])
            elif sub in ("名单", "列表", "list", "查"):
                await self._cmd_list_mutes(event)
            elif sub in ("历史", "history", "记录", "流水"):
                await self._cmd_list_mute_history(event, ["/禁言历史"] + parts[2:])
            else:
                await self._cmd_mute(event, parts)
        elif cmd == "/审批":
            sub = parts[1].lower() if len(parts) > 1 else ""
            if sub in ("通过", "同意", "approve", "过"):
                await self._cmd_review(event, "/通过", ["/通过"] + parts[2:])
            elif sub in ("拒绝", "驳回", "reject", "拒"):
                await self._cmd_review(event, "/拒绝", ["/拒绝"] + parts[2:])
            else:
                await self._cmd_list_requests(event, parts)
        elif cmd == "/状态":
            await self._cmd_status(event, parts)
        elif cmd == "/撤销":
            await self._cmd_revoke(event, parts)
        elif cmd == "/撤回":
            await self._cmd_recall(event, parts)
        elif cmd == "/管理登记":
            await self._cmd_admin_register(event, parts)
        elif cmd == "/成员":
            await self._cmd_member(event, parts)

    # ------------------------------------------------------------------
    # /成员：群成员档案
    # ------------------------------------------------------------------
    async def _cmd_member(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """群成员档案：列表 / 改名 / 备注 / 删除 / 清理 / 拉取 / 查。

        官方事件与消息里只有 member_openid、没有昵称，所以这张表得靠人工维护；
        面板「成员档案」页签改的是同一份数据（members.json）。
        """
        g = event.get_group_id()
        if not g:
            await self._reply(event, "这个指令要在群里发哈。")
            return
        if not self._members_on():
            await self._reply(event, "群成员档案已在配置里关闭（member_registry.enabled）。")
            return

        sub = parts[1].lower() if len(parts) > 1 else ""
        args = parts[2:]

        # 宽松一点：/群管 成员 2、/群管 成员 已退 都当作列表查询
        if sub.isdigit() or sub in ("已退", "退群", "在群", "全部", "无名", "未命名", "有名", "named"):
            args = [sub] + args
            sub = "列表"

        if sub in ("", "列表", "list"):
            status = "all"
            page = 1
            for a in args:
                if a in ("在群", "active"):
                    status = "active"
                elif a in ("已退", "退群", "left"):
                    status = "left"
                elif a in ("无名", "未命名", "unnamed"):
                    status = "unnamed"
                elif a in ("有名", "named"):
                    status = "named"
                elif a.isdigit():
                    page = max(1, int(a))
            await self._reply(event, self._render_member_list(g, status=status, page=page))
            return

        if sub in ("查", "搜索", "search"):
            kw = " ".join(args).strip()
            if not kw:
                await self._reply(event, "用法：/群管 成员 查 <昵称/备注/openid 片段>")
                return
            await self._reply(event, self._render_member_list(g, keyword=kw, title=f"搜索「{kw}」"))
            return

        if sub in ("改名", "设置", "rename", "set"):
            if len(args) < 2:
                await self._reply(event, "用法：/群管 成员 改名 <序号或openid> <昵称>\n先发 /群管 成员 列表 看序号。")
                return
            openid = self._member_ref(g, args[0])
            if not openid:
                await self._reply(event, f"没找到成员「{args[0]}」，先发 /群管 成员 列表 看看序号。")
                return
            name = " ".join(args[1:]).strip()[:40]
            self.members.set_name(g, openid, name, source="manual")
            await self._reply(event, f"✅ 已记下：{member_registry_lib.short_id(openid)} → {name or '（清空）'}")
            return

        if sub in ("备注", "note"):
            if len(args) < 1:
                await self._reply(event, "用法：/群管 成员 备注 <序号或openid> [备注内容]（留空即清除）")
                return
            openid = self._member_ref(g, args[0])
            if not openid:
                await self._reply(event, f"没找到成员「{args[0]}」。")
                return
            note = " ".join(args[1:]).strip()[:200]
            self.members.set_note(g, openid, note)
            await self._reply(event, f"✅ 备注已更新：{member_registry_lib.short_id(openid)} → {note or '（空）'}")
            return

        if sub in ("删除", "删", "delete", "del"):
            if not args:
                await self._reply(event, "用法：/群管 成员 删除 <序号或openid>")
                return
            openid = self._member_ref(g, args[0])
            if not openid:
                await self._reply(event, f"没找到成员「{args[0]}」。")
                return
            self.members.delete(g, openid)
            await self._reply(event, f"🗑 已从档案删除：{member_registry_lib.short_id(openid)}")
            return

        if sub in ("清理", "clean"):
            n = self.members.delete_left(g)
            await self._reply(event, f"🧹 已清理 {n} 条「已退群」档案。" if n else "没有「已退群」的档案需要清理。")
            return

        if sub in ("拉取", "同步", "sync"):
            await self._reply(event, await self._cmd_member_sync(g))
            return

        await self._reply(
            event,
            "群成员档案用法：\n"
            "/群管 成员 —— 看档案（默认按最近发言排序）\n"
            "/群管 成员 已退 / 无名 / 全部 —— 按状态筛\n"
            "/群管 成员 查 <关键词> —— 搜昵称/备注/openid\n"
            "/群管 成员 改名 <序号> <昵称> —— 人工补名字\n"
            "/群管 成员 备注 <序号> <备注>\n"
            "/群管 成员 删除 <序号> —— 删单条\n"
            "/群管 成员 清理 —— 清掉所有已退群记录\n"
            "/群管 成员 拉取 —— 调官方群成员接口补昵称（需白名单权限）\n"
            "改昵称也可以走网页面板「成员档案」页签。",
        )

    def _member_ref(self, group_openid: str, token: str) -> str:
        """把「序号」或「openid / openid 尾段」解析成完整 openid。"""
        tok = str(token or "").strip()
        if not tok:
            return ""
        if tok.isdigit() and len(tok) <= 4:
            rows, _ = self.members.list_members(group_openid, limit=200)
            idx = int(tok)
            if 1 <= idx <= len(rows):
                return str(rows[idx - 1].get("openid") or "")
            # 超出范围就不要再当序号了，下面继续按 openid / 尾段找
        if self.members.has(group_openid, tok):
            return tok
        rows, _ = self.members.list_members(group_openid, keyword=tok, limit=5)
        if len(rows) == 1:
            return str(rows[0].get("openid") or "")
        return ""

    def _render_member_list(self, group_openid: str, *, status: str = "all",
                            keyword: str = "", page: int = 1, title: str = "") -> str:
        """档案列表文本（每页 15 条）。"""
        per = 15
        rows, total = self.members.list_members(
            group_openid, status=status, keyword=keyword,
            offset=(page - 1) * per, limit=per,
        )
        st = self.members.stats(group_openid)
        head = title or {
            "active": "在群成员",
            "left": "已退群成员",
            "unnamed": "还没名字的成员",
            "named": "已命名成员",
        }.get(status, "全部成员")
        lines = [
            f"📇 {head}（共 {st['total']} 人：在群 {st['active']} / 已退 {st['left']}；"
            f"有名字 {st['named']}，累计发言 {st['messages']} 条）"
        ]
        if not rows:
            lines.append("这一页没有记录。档案是随着群消息自动攒的，机器人没收到过消息就不会有记录。")
            return "\n".join(lines)
        base = (page - 1) * per
        for i, r in enumerate(rows, start=1):
            name = str(r.get("nickname") or "").strip()
            label = name or f"（未命名 {member_registry_lib.short_id(r.get('openid'))}）"
            note = str(r.get("note") or "").strip()
            last = float(r.get("last_seen") or 0)
            when = (
                datetime.fromtimestamp(last, _active_tz()).strftime("%m-%d %H:%M")
                if last else "-"
            )
            flag = " · 已退群" if r.get("status") == "left" else ""
            extra = f" · 备注：{note}" if note else ""
            lines.append(
                f"{base + i}. {label} · 发言 {int(r.get('msg_count') or 0)} · 末次 {when}{flag}{extra}"
            )
        pages = max(1, (total + per - 1) // per)
        if pages > 1:
            lines.append(f"—— 第 {page}/{pages} 页 · 下一页：/群管 成员 列表 {page + 1}")
        lines.append("改名：/群管 成员 改名 <序号> <昵称>")
        return "\n".join(lines)

    async def _cmd_member_sync(self, group_openid: str) -> str:
        """尝试用官方群成员接口补昵称。

        注意：GET /v2/groups/{openid}/members 与 .../members/{openid}
        目前都是**内邀白名单**能力，普通机器人调用返回 11253。这里如实回报，
        不做"假装成功"的提示。
        """
        api = self._api_for_group_or_default(group_openid)
        if api is None:
            return "没有可用的机器人实例，先确认 QQ 官方适配器在线。"
        try:
            members = await api.list_group_members(group_openid)
        except QQApiError as e:
            if e.code == 11253:
                return (
                    "❌ 官方「群成员列表」接口还是内邀能力（错误码 11253），本机器人没有权限。\n"
                    "可以先用 /群管 成员 改名 手工补，或在面板「成员档案」页签里批量改。"
                )
            return f"❌ 拉取失败：{e}"
        except Exception as e:  # noqa: BLE001
            return f"❌ 拉取失败：{e}"
        if not members:
            return "接口没返回成员（可能权限不足或群为空）。"
        rows = [
            {
                "openid": m.get("member_openid") or "",
                "nickname": m.get("username") or "",
                "role": m.get("member_role") or "",
                "user_openid": m.get("union_openid") or m.get("user_openid") or "",
                "joined_at": m.get("joined_at") or "",
            }
            for m in members
        ]
        added, updated = self.members.upsert_many(group_openid, rows)
        return (
            f"✅ 已从官方接口同步 {len(rows)} 人（新增 {added}、更新 {updated}）。\n"
            f"人工改过的昵称不会被覆盖。"
        )

    async def _cmd_group_name(self, event: AstrMessageEvent) -> None:
        """刷新当前群的群名（管理员）。

        群名接口 GET /v2/groups/{openid}/info 仅白名单机器人可用（11253），
        无权限时如实回报，并提示改用面板手动备注。
        """
        grp = event.get_group_id()
        if not grp:
            await self._reply(event, "这个指令要在群里发哈。")
            return
        pid = event.get_platform_id()
        try:
            api = self._get_api(pid)
        except QQApiError as e:
            await self._reply(event, f"没找到这个机器人的实例：{e}")
            return
        data, err = await api.fetch_group_info(grp)
        if data and data.get("group_name"):
            name = str(data["group_name"])[:60]
            self.mod.set_group_meta(grp, name=name, platform_id=pid,
                                    name_state="ok", name_error="")
            await self._reply(event, f"本群群名：{name}")
            return
        code = getattr(err, "code", None)
        msg = str(err) if err else "未知错误"
        self.mod.set_group_meta(grp, platform_id=pid,
                                name_state=("denied" if code == 11253 else "failed"),
                                name_error=msg[:200])
        if code == 11253:
            await self._reply(event, "取不到群名：官方「获取群信息」接口只对白名单机器人开放"
                                     "（错误码 11253，需在 QQ 开放平台申请「群基本信息」权限）。\n"
                                     "临时办法：打开管理面板 →「群规审核」→ 点「✎ 备注」给这个群起个名字。")
        else:
            await self._reply(event, f"拉取群名失败：{msg}")

    def _is_mentioned_bot(self, event: AstrMessageEvent) -> bool:
        """判断消息是否 @ 了机器人自身（用于不带 / 的指令识别）。"""
        try:
            raw = event.message_obj.raw_message
            mentions = getattr(raw, "mentions", None) or []
            for u in mentions:
                if getattr(u, "is_you", False) and (
                    getattr(u, "member_openid", None)
                    or getattr(u, "openid", None)
                    or getattr(u, "id", None)
                ):
                    return True
        except Exception:
            pass
        return False

    # ---------- 禁言 ----------
    def _mentioned_openids(self, event: AstrMessageEvent) -> list[str]:
        out: list[str] = []
        try:
            # QQ 官方 API 的 mentions 包含所有被 @ 的对象（含机器人自身），
            # 必须排除机器人自己，否则会尝试禁言机器人而报 40103004。
            self_id = event.get_self_id()
            raw = event.message_obj.raw_message
            mentions = getattr(raw, "mentions", None) or []
            for u in mentions:
                oid = (
                    getattr(u, "member_openid", None)
                    or getattr(u, "openid", None)
                    or getattr(u, "id", None)
                )
                if oid and oid != self_id:
                    out.append(oid)
        except Exception:
            pass
        return out

    async def _cmd_mute(self, event: AstrMessageEvent, parts: list[str]) -> None:
        api = self._get_api(event.get_platform_id())
        g = event.get_group_id()
        mentioned = self._mentioned_openids(event)
        if not mentioned:
            await self._reply(event, self._render("msg_mute_need_at"))
            return
        # 时长与理由：第一个能解析为时长的参数作为时长，
        # 其余非 @ 参数全部合并为理由（兼容 "10分钟 刷屏" 与 "刷屏 10分钟" 两种顺序）
        seconds: Optional[int] = None
        reason_parts: list[str] = []
        for p in parts[1:]:
            if p.startswith("@"):
                continue
            if seconds is None:
                s = parse_duration(p)
                if s is not None:
                    seconds = s
                    continue
            reason_parts.append(p)
        if seconds is None:
            await self._reply(event, self._render("msg_duration_invalid"))
            return
        if seconds <= 0:
            await self._reply(event, self._render("msg_duration_invalid"))
            return
        # 官方上限 30 天：超出按上限执行，回复里显示的就是真实时长
        if seconds > 30 * 86400:
            self.logger.info(f"禁言时长 {seconds}s 超过官方上限，按 30 天执行")
            seconds = 30 * 86400
        reason = " ".join(reason_parts).strip() or str(
            self._read_config_value(("basic", "default_mute_reason"), default="违反群规", legacy_keys=("default_mute_reason",))
        )
        target = mentioned[0]
        if target == event.get_sender_id():
            await self._reply(event, self._render("msg_mute_self"))
            return
        expire_ts = time.time() + seconds
        expire_at = QQApi.format_rfc3339(expire_ts)
        self.logger.info(
            f"禁言: group={g} member={target} seconds={seconds} "
            f"reason={reason!r} expire={expire_at}"
        )
        try:
            await api.set_mute(g, target, mute_expire_at=expire_at, op="add")
        except QQApiError as e:
            self.logger.error(f"禁言失败: {e}")
            await self._reply(event, self._render("msg_mute_failed", error=str(e)))
            return
        # 记录历史（被禁言者昵称 API 不返回，存 openid；操作者昵称单独存）
        self._add_mute_record(
            group_openid=g,
            platform_id=event.get_platform_id(),
            member_openid=target,
            username="",
            reason=reason,
            seconds=seconds,
            expire_ts=expire_ts,
            operator=event.get_sender_id(),
            operator_name=event.get_sender_name() or "",
        )
        await self._reply(
            event,
            self._render(
                "msg_mute_success",
                username=f"<@{target}>",
                at=target,
                seconds=seconds,
                minutes=seconds // 60,
                duration_text=fmt_duration(seconds),
                reason=reason,
            ),
        )

    async def _cmd_unmute(self, event: AstrMessageEvent, parts: list[str]) -> None:
        api = self._get_api(event.get_platform_id())
        g = event.get_group_id()
        mentioned = self._mentioned_openids(event)
        if not mentioned:
            await self._reply(event, self._render("msg_mute_need_at"))
            return
        for target in mentioned:
            try:
                await api.set_mute(g, target, op="del")
                self._mark_unmuted(g, target)
                self.logger.info(f"解禁: group={g} member={target}")
                await self._reply(event, self._render("msg_unmute_success", username=f"<@{target}>", at=target))
            except QQApiError as e:
                self.logger.error(f"解禁失败: {e}")
                await self._reply(event, self._render("msg_mute_failed", error=str(e)))

    async def _cmd_recall(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """撤回消息。

        用法:
        - 引用（回复）目标消息后发送 /撤回
        - /撤回 <message_id>

        官方限制: 发送超过 2 分钟的消息不可撤回；机器人是群管理员时可撤回
        普通群成员的消息（消息 ID 从群消息事件 d.id 获取），否则只能撤回
        自己发送的消息。
        """
        # 功能开关：配置里关闭后指令直接提示（不误报为无权限）
        if not bool(self._read_config_value(
                ("recall", "enable_recall"), default=True,
                legacy_keys=("enable_recall",))):
            await self._reply(event, self._render("msg_recall_disabled"))
            return

        api = self._get_api(event.get_platform_id())
        g = event.get_group_id()
        if not g:
            await self._reply(event, self._render("msg_recall_group_only"))
            return

        message_id = ""
        source = "quote"
        quoted_content = ""
        # 方式一：/撤回 <message_id>
        # 兼容 ROBOT1.0_xxx / REFIDX_xxx== 等官方消息 ID 格式
        if len(parts) > 1 and re.fullmatch(r"[0-9A-Za-z_.\-=]{16,}", parts[1]):
            message_id = parts[1]
            source = "message_id"
        # 方式二：引用（回复）目标消息
        else:
            try:
                from astrbot.api.message_components import Reply

                reply = next(
                    (c for c in event.message_obj.message if isinstance(c, Reply)),
                    None,
                )
                if reply and getattr(reply, "id", None):
                    message_id = str(reply.id)
                    # 引用组件里带被引用消息的原文（官方接口不提供查消息内容，
                    # 引用撤回是唯一能拿到原文的途径），用于历史备份查验。
                    quoted_content = self._clean_quote_summary(getattr(reply, "message_str", "") or "")
                    if not quoted_content:
                        chain = getattr(reply, "chain", None)
                        if chain:
                            quoted_content = "".join(
                                str(getattr(c, "text", "") or "")
                                for c in chain
                            )
                # 无论 Reply 组件是否带原文/ID，都从原始消息里再挖一次：
                # botpy 适配器可能拿不到引用 ID（QQ 官方引用消息的被引用
                # ID 在 msg_elements[0].msg_idx，见 _patch_botpy_quote_fields），
                # 且原文用于下面的缓存匹配（真实消息 ID 只能靠它找回）。
                raw = getattr(event.message_obj, "raw_message", None)
                if raw is not None:
                    mid, mcontent = self._extract_quote_from_raw(raw)
                    if not message_id and mid:
                        message_id = str(mid)
                    if not quoted_content and mcontent:
                        quoted_content = str(mcontent)

                # 关键：QQ 官方引用消息事件拿不到被引用消息的 d.id（只有
                # REFIDX 引用索引，撤回 API 不认），所以优先用原文在消息缓存
                # 里匹配真实消息 ID（机器人能收到群内全量消息时必然命中）。
                if quoted_content:
                    cache_id = self._match_recent_msg_id(g, quoted_content)
                    # 仅当现有 ID 是引用索引（REFIDX/TMP，撤回 API 不接受）
                    # 或为空时，才用缓存中的真实消息 ID 覆盖。
                    if cache_id and (
                        not message_id
                        or message_id.startswith("REFIDX")
                        or message_id.startswith("TMP_")
                    ):
                        message_id = cache_id
                        source = "quote_cache"
                        self.logger.debug(
                            f"引用撤回命中消息缓存: text={quoted_content!r} id={cache_id}"
                        )
                    elif message_id and not cache_id:
                        self.logger.debug(
                            f"引用撤回未命中缓存，将尝试事件内 ID: {message_id} "
                            f"(REFIDX 格式可能不被撤回 API 接受)"
                        )
            except Exception as e:
                self.logger.debug(f"提取引用消息 id 失败: {e}")

        if not message_id:
            await self._reply(event, self._render("msg_recall_need_quote"))
            return

        try:
            await api.recall_group_message(g, message_id)
            self.logger.info(f"撤回消息成功: group={g} message_id={message_id}")
            # 永久保存原始消息，便于备份查验
            rec = self._add_recall_record(
                group_openid=g,
                platform_id=event.get_platform_id(),
                message_id=message_id,
                content=quoted_content,
                source=source,
                operator=event.get_sender_id(),
                operator_name=event.get_sender_name() or "",
            )
            self.logger.info(
                f"已记录撤回历史: id={rec['id']} source={source} "
                f"content={'有原文' if quoted_content else '无原文(仅消息ID)'}"
            )
            await self._reply(event, self._render("msg_recall_ok"))
        except QQApiError as e:
            self.logger.warning(
                f"撤回消息失败: group={g} message_id={message_id} err={e}"
            )
            if e.code == 40064004:
                await self._reply(event, self._render("msg_recall_timeout"))
            elif e.code == 40062003:
                await self._reply(event, self._render("msg_recall_no_perm"))
            elif e.code == 40061001:
                await self._reply(event, self._render("msg_recall_invalid_id"))
            else:
                await self._reply(event, self._render("msg_recall_failed", error=str(e)))

    def _extract_quote_from_raw(self, raw) -> tuple[str, str]:
        """从 botpy 原始消息对象中提取被引用消息的 ID 与原文。

        QQ 官方群消息引用事件（message_type=103）结构：
        - msg_elements[0].msg_idx: 被引用消息 ID（REFIDX_xxx== 格式）
        - msg_elements[0].content: 被引用消息原文
        - message_scene.ext: ["msg_idx=...", "ref_msg_idx=...", ...]
        - message_reference.message_id: 部分场景下直接携带

        返回 (message_id, content)，取不到时为空字符串；解析异常不抛出。
        """
        mid = ""
        content = ""
        try:
            elems = getattr(raw, "msg_elements", None) or []
            scene = getattr(raw, "message_scene", None) or {}
            ref = getattr(raw, "message_reference", None) or {}

            # 调试：完整 dump 引用事件的关键字段，确认真实 payload 结构
            try:
                import json as _json

                def _d(v):
                    if isinstance(v, (dict, list)):
                        try:
                            return _json.dumps(v, ensure_ascii=False)
                        except Exception:
                            return str(v)
                    return str(v)

                self.logger.debug(
                    "引用消息原始结构: "
                    f"id={getattr(raw, 'id', '')!r} "
                    f"message_type={getattr(raw, 'message_type', '')!r} "
                    f"msg_elements={_d(elems)} "
                    f"message_scene={_d(scene)} "
                    f"message_reference={_d(ref)}"
                )
            except Exception:
                pass

            # 1) 引用元素里直接带被引用消息 ID（REFIDX 格式）与原文
            if elems:
                e0 = elems[0]
                if isinstance(e0, dict):
                    mid = str(
                        e0.get("msg_idx") or e0.get("id") or e0.get("message_id") or ""
                    )
                    content = self._clean_quote_summary(str(e0.get("content") or ""))
                else:
                    mid = str(
                        getattr(e0, "msg_idx", "")
                        or getattr(e0, "id", "")
                        or getattr(e0, "message_id", "")
                    )
                    content = cls._clean_quote_summary(str(getattr(e0, "content", "") or ""))
            # 2) message_reference 直接携带被引用消息 ID
            if not mid:
                if isinstance(ref, dict):
                    mid = str(ref.get("message_id") or "")
                else:
                    mid = str(getattr(ref, "message_id", "") or "")
            # 3) message_scene.ext 里找 ref_msg_idx / msg_idx
            if not mid:
                if isinstance(scene, dict):
                    ext = scene.get("ext", []) or []
                else:
                    ext = getattr(scene, "ext", None) or []
                for kv in ext:
                    if isinstance(kv, str) and kv.startswith("ref_msg_idx="):
                        mid = kv.split("=", 1)[1]
                        break
                if not mid:
                    for kv in ext:
                        if isinstance(kv, str) and kv.startswith("msg_idx="):
                            mid = kv.split("=", 1)[1]
                            break
        except Exception as e:
            self.logger.debug(f"解析引用消息原始结构失败: {e}")
        return mid, content

    async def _cmd_list_mutes(self, event: AstrMessageEvent) -> None:
        api = self._get_api(event.get_platform_id())
        g = event.get_group_id()
        try:
            data = await api.get_mute_state(g)
        except QQApiError as e:
            self.logger.error(f"查询禁言失败: {e}")
            await self._reply(event, self._render("msg_api_error", error=str(e)))
            return
        members = data.get("members") or []
        if not members:
            await self._reply(event, self._render("msg_mute_list_empty"))
            return
        history = list(self._mute_history.get(g, []))
        lines = []
        for m in members:
            oid = m.get("member_openid", "?")
            # QQ API 不返回成员昵称（username 字段基本为空），统一用短标识；
            # 匹配/判断始终用完整 openid，短标识仅用于人眼区分。
            name = (m.get("username") or "").strip() or f"{oid[:8]}"
            expire = fmt_rfc3339(m.get("mute_expire_at", ""))
            reason = self._last_reason(g, oid)
            cnt = sum(1 for r in history if r.get("member_openid") == oid)
            line = f"· {name} 到期 {expire}"
            if reason:
                line += f"（理由：{reason}）"
            if cnt:
                line += f" 历史{cnt}次"
            lines.append(line)
        await self._reply(event, self._render("msg_mute_list", list="\n".join(lines)))

    @staticmethod
    def _mute_status_text(status: str) -> str:
        return {"active": "禁言中", "unmuted": "已解禁", "expired": "已到期"}.get(status, "")

    def _aggregate_member_stats(self, records: list[dict]) -> list[dict]:
        """按成员聚合禁言记录：次数、最近时间/状态/理由。禁言中优先、次数倒序。"""
        stats: dict[str, dict] = {}
        for r in records:
            oid = r.get("member_openid") or "?"
            st = stats.setdefault(
                oid,
                {"openid": oid, "count": 0, "last": 0, "status": "", "last_reason": ""},
            )
            st["count"] += 1
            ts = r.get("mute_at") or 0
            if ts >= st["last"]:
                st["last"] = ts
                st["status"] = self._effective_mute_status(r)
                st["last_reason"] = r.get("reason") or ""
        out = list(stats.values())
        out.sort(key=lambda s: (s["status"] != "active", -s["count"]))
        return out

    def _render_mute_summary(self, records: list[dict]) -> str:
        """按成员聚合 -> 多行文本块。"""
        blocks = []
        for s in self._aggregate_member_stats(records):
            st = self._mute_status_text(s["status"])
            last_t = fmt_ts(s["last"], "%m-%d %H:%M") if s["last"] else "?"
            reason = s["last_reason"] or "（无理由）"
            blocks.append(
                f"· {s['openid'][:8]} 累计{s['count']}次 最近:{last_t}\n"
                f"  理由：{reason} ｜ 状态：{st}"
            )
        return "\n\n".join(blocks)

    def _fmt_history_line(self, r: dict, full: bool = False) -> str:
        """单条禁言记录 -> 多行文本块（时间/成员/时长一行，理由、状态/操作各一行）。

        full=True 时展示完整 openid（单用户查询用，便于确认身份）。
        """
        st = self._mute_status_text(self._effective_mute_status(r))
        reason = r.get("reason") or "（无理由）"
        op = r.get("operator_name") or (r.get("operator") or "")[:8]
        ts = fmt_ts(r.get("mute_at", 0), "%m-%d %H:%M")
        oid = r.get("member_openid") or "?"
        if not full:
            oid = oid[:8]
        dur = r.get("duration_text") or "?"
        return (
            f"#{r.get('id')} {ts} {oid} 时长{dur}\n"
            f"  理由：{reason}\n"
            f"  状态：{st} ｜ 操作：{op}"
        )

    @staticmethod
    def _match_openid(oid: str, target: str) -> bool:
        """openid 匹配：完整相等或前缀匹配（不区分大小写）。"""
        if not oid or not target:
            return False
        oid, target = oid.lower(), target.lower()
        return oid == target or oid.startswith(target)

    async def _cmd_list_mute_history(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """/禁言历史 [条数|汇总] [@用户|openid前缀]：本群流水（默认10条）/ 按人汇总 / 单用户记录。

        单用户查询示例：
          /禁言历史 @某人        -> 该用户最近10条
          /禁言历史 @某人 5      -> 该用户最近5条
          /禁言历史 @某人 汇总   -> 该用户禁言汇总
          /禁言历史 4F697792...  -> 直接输 openid（或前缀）查询，方便查已不在群的用户
        """
        g = event.get_group_id()
        limit = 10
        mode = "group"
        target: Optional[str] = None
        mentioned = self._mentioned_openids(event)
        if mentioned:
            target = mentioned[0]
        for p in parts[1:]:
            if p.isdigit():
                limit = max(1, min(int(p), 50))
            elif p in ("汇总", "summary", "统计"):
                mode = "summary"
            elif target is None and len(p) >= 8 and re.fullmatch(r"[0-9A-Za-z_-]+", p):
                # 未 @ 时支持直接输入 openid 或前缀（条数参数已在上面被 isdigit 捕获）
                target = p
        records = list(self._mute_history.get(g, []))
        if not records:
            await self._reply(event, self._render("msg_mute_history_empty"))
            return

        if target:
            matched = [r for r in records if self._match_openid(r.get("member_openid", ""), target)]
            if not matched:
                await self._reply(
                    event,
                    self._render("msg_mute_history_user_empty", openid=target),
                )
                return
            if mode == "summary":
                await self._reply(
                    event,
                    self._render(
                        "msg_mute_history_user_summary",
                        openid=target,
                        count=len(matched),
                        list=self._render_mute_summary(matched),
                    ),
                )
                return
            matched = matched[-limit:][::-1]  # 最新在前
            blocks = [self._fmt_history_line(r, full=True) for r in matched]
            await self._reply(
                event,
                self._render(
                    "msg_mute_history_user",
                    openid=target,
                    count=len(matched),
                    list="\n\n".join(blocks),
                ),
            )
            return

        if mode == "summary":
            await self._reply(
                event,
                self._render(
                    "msg_mute_history_summary",
                    count=len(records),
                    list=self._render_mute_summary(records),
                ),
            )
            return
        records = records[-limit:][::-1]  # 最新在前
        blocks = [self._fmt_history_line(r) for r in records]
        await self._reply(
            event,
            self._render("msg_mute_history_list", count=len(records), list="\n\n".join(blocks)),
        )

    # ---------- 入群审批 ----------
    def _pending_list(self, group_openid: str) -> list[dict]:
        return self._pending.get(group_openid) or []

    @staticmethod
    def _req_key(r: dict) -> tuple:
        """入群申请去重键。

        QQ 官方接口的 join_request_id 每次查询返回值都会变化（实测同一申请
        每次 GET 都不相同），不能用于去重；改用 (member_openid, apply_at)
        作为稳定键。
        """
        return (r.get("member_openid") or "", r.get("apply_at") or "")

    async def _refresh_requests(self, group_openid: str, platform_id: str) -> list[dict]:
        """拉取最新申请并更新缓存，返回当前待审批列表。"""
        # 注意：本方法内部会更新 self._pending[group_openid]，
        # 调用方若需对比"新申请"，必须先保存上一轮缓存再调用。
        # _get_api 也可能抛 QQApiError（平台实例不存在 / 未配置 appid），
        # 必须一起包在 try 里：否则汇总模式下「某一个群的机器人已下线」
        # 会让整个 /requests 接口 500，前端整页显示「加载失败」。
        try:
            api = self._get_api(platform_id)
            data = await api.get_join_request_list(group_openid, limit=20)
            items = data.get("list") or []
            self._pending[group_openid] = items[-50:]
            self._join_error_code[group_openid] = None
            return items
        except QQApiError as e:
            self._join_error_code[group_openid] = e.code
            if e.code == 11703:
                # 机器人不是群管理员：这是权限前置条件，不是 bug，
                # 轮询侧会退避，这里只留一条 warning。
                self.logger.warning(
                    f"群 {group_openid} 拉取入群申请失败: 机器人不是群管理员（11703），已暂停该群轮询"
                )
            else:
                self.logger.error(f"拉取入群申请失败: {e}")
            return self._pending_list(group_openid)

    def _fmt_request_line(self, idx: int, r: dict) -> str:
        return f"{idx}. {self._request_brief(r)}"

    # 待处理列表每页条数（被动回复，一条消息塞太多会被平台截断）
    REQUEST_PAGE_SIZE = 10

    @staticmethod
    def _request_brief(r: dict) -> str:
        """申请的一行摘要（不含序号）：昵称 (来源 时间) 验证消息。"""
        name = r.get("username") or r.get("member_openid") or "?"
        t = fmt_rfc3339(r.get("apply_at", ""))
        source = "邀请" if r.get("apply_source") == "invited" else "主动"
        brief = f"{name} ({source} {t})"
        try:
            vi = r.get("verify_info") or {}
            if vi.get("method") == "verify_message" and vi.get("verify_message"):
                brief += f" 验证消息: {vi['verify_message']}"
        except Exception:  # noqa: BLE001
            pass
        return brief

    async def _cmd_list_requests(
        self, event: AstrMessageEvent, parts: list[str] | None = None
    ) -> None:
        """待处理入群申请：本群 / 全部群，可翻页。

        用法：
            /群管 审批            本群第 1 页
            /群管 审批 2          本群第 2 页
            /群管 审批 全部       所有已知群汇总
            /群管 审批 全部 2     汇总的第 2 页
        """
        g = event.get_group_id()
        args = [a for a in (parts or [])[1:]]
        all_groups = any(a in ("全部", "全群", "所有", "all") for a in args)
        page = 1
        for a in args:
            if a.isdigit():
                page = max(1, int(a))
        if all_groups:
            await self._list_requests_all_groups(event, page)
            return

        pending = self._pending_list(g)
        if not pending:
            pending = await self._refresh_requests(g, event.get_platform_id())
        if not pending:
            await self._reply(event, self._render("msg_requests_empty"))
            return
        per = self.REQUEST_PAGE_SIZE
        total = len(pending)
        pages = max(1, (total + per - 1) // per)
        page = min(page, pages)
        chunk = pending[(page - 1) * per: page * per]
        lines = [f"（本群 {total} 条，第 {page}/{pages} 页）"]
        for i, r in enumerate(chunk, start=(page - 1) * per + 1):
            # 序号用全局序号，保证 /群管 通过 N 与本列表一一对应
            lines.append(self._fmt_request_line(i, r))
        if page < pages:
            lines.append(f"下一页：/群管 审批 {page + 1}")
        await self._reply(event, self._render("msg_requests_list", list="\n".join(lines)))

    async def _list_requests_all_groups(self, event: AstrMessageEvent, page: int) -> None:
        """按群汇总所有待处理申请（被动回复）。

        为什么要这个：群内通知受内联键盘行数、消息长度限制，一个群申请多了
        根本看不全；管理员想一次看完全部群的情况，只能翻面板。
        """
        groups = self._managed_groups()
        if not groups:
            await self._reply(event, self._render("msg_requests_empty"))
            return
        rows: list[str] = []
        total = 0
        for gid, pid in groups.items():
            try:
                items = await self._refresh_requests(gid, pid)
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"[审批] 汇总时拉取群 {gid} 失败: {e}")
                items = self._pending_list(gid)
            if not items:
                continue
            name = self.mod.display_name(gid) or gid[-6:]
            total += len(items)
            rows.append(f"【{name}】{len(items)} 条")
            for i, r in enumerate(items, start=1):
                rows.append(f"  {i}. {self._request_brief(r)}")
            rows.append("  处理：/群管 通过 序号（在该群发）")
        if not rows:
            await self._reply(event, self._render("msg_requests_empty"))
            return
        per = 30
        pages = max(1, (len(rows) + per - 1) // per)
        page = min(max(1, page), pages)
        chunk = rows[(page - 1) * per: page * per]
        head = f"（全部群汇总：{len(groups)} 个群，共 {total} 条，第 {page}/{pages} 页）"
        body = "\n".join([head] + chunk)
        if page < pages:
            body += f"\n下一页：/群管 审批 全部 {page + 1}"
        await self._reply(event, self._render("msg_requests_list", list=body))

    async def _cmd_report(self, event: AstrMessageEvent) -> None:
        """被动版日报：/群管 汇报 —— 立刻回一条，不等定时任务。

        和定时汇报共用 _build_report_text，区别只是「回复当前消息」而不是主动推送：
        主动消息要过频控、还依赖群未关闭主动消息，手动查的时候用被动回复更稳。
        """
        g = event.get_group_id()
        if not g:
            await self._reply(event, "这个指令要在群里发哈。")
            return
        text = await self._build_report_text(g, event.get_platform_id())
        await self._reply(event, text)

    async def _cmd_review(self, event: AstrMessageEvent, cmd: str, parts: list[str]) -> None:
        g = event.get_group_id()
        pending = self._pending_list(g)
        if not pending:
            pending = await self._refresh_requests(g, event.get_platform_id())
        if not pending:
            await self._reply(event, self._render("msg_requests_empty"))
            return
        if len(parts) < 2:
            await self._reply(event, self._render("msg_request_index_invalid"))
            return
        arg = parts[1]
        # 序号 或 昵称关键字
        idx: Optional[int] = None
        if arg.isdigit():
            idx = int(arg)
            if not (1 <= idx <= len(pending)):
                await self._reply(event, self._render("msg_request_index_invalid"))
                return
        else:
            for i, r in enumerate(pending, start=1):
                if arg.lower() in str(r.get("username", "")).lower():
                    idx = i
                    break
            if idx is None:
                await self._reply(event, self._render("msg_request_index_invalid"))
                return

        target = pending[idx - 1]
        member_openid = target.get("member_openid", "")
        username = target.get("username", member_openid)
        reason = " ".join(parts[2:]) if len(parts) > 2 else None
        op = "approve" if cmd == "/通过" else "decline"

        ok, err = await self._apply_join_decision(
            event, g, event.get_platform_id(), target, op, reason=reason, via="指令"
        )
        if not ok:
            await self._reply(event, self._render("msg_approve_failed", error=err))
            return
        if op == "approve":
            await self._reply(
                event, self._render("msg_approve_success", username=username)
            )
            welcome_left = await self._welcome_after_approve(
                event, g, event.get_platform_id(), member_openid, username
            )
            if welcome_left:
                await self._reply(event, welcome_left)
        else:
            await self._reply(
                event, self._render("msg_reject_success", username=username)
            )

    async def _apply_join_decision(
        self, event: Optional[AstrMessageEvent], group_openid: str, platform_id: str,
        req: dict, op: str, *, reason: str | None = None, via: str = "指令",
    ) -> tuple[bool, str]:
        """执行一次入群审批，并同步本地缓存（指令与按钮**共用这一份实现**）。

        在 1.4.0 之前，审批逻辑只存在于 ``_cmd_review`` 里；消息按钮上线后如果
        再复制一份，两边很容易漂移（比如一边删了缓存、另一边没删，导致申请
        反复出现在列表里）。所以这里下沉成唯一出口。

        Args:
            event: 来源事件，可为 None（按钮路径也会传，仅用于日志/展示）。
            req: 申请条目（至少含 ``member_openid`` / ``join_request_id``）。
            op: ``approve`` / ``decline``。
            via: 日志里的来源标记（指令 / 按钮）。

        Returns:
            ``(是否成功, 错误描述)``。
        """
        member_openid = str(req.get("member_openid") or "")
        join_request_id = str(req.get("join_request_id") or "")
        api = self._get_api(platform_id)
        try:
            await api.approve_join_request(
                group_openid, member_openid, join_request_id,
                op=op, reject_reason=reason,
            )
        except QQApiError as e:
            self.logger.error(f"[审批] 失败（{via}）: {e} group={group_openid[-6:]}")
            return False, str(e)
        # 从待审批缓存移除，并清掉通知去重键——否则这条申请被处理掉之后，
        # 轮询仍可能因为去重表还没过期而反复提示。
        self._pending[group_openid] = [
            x for x in (self._pending.get(group_openid) or [])
            if self._req_key(x) != self._req_key(req)
        ]
        self._notified.get(group_openid, {}).pop(self._req_key(req), None)
        self.logger.info(
            f"[审批] 完成（{via}）: group={group_openid[-6:]} op={op} "
            f"member={member_openid[-6:]} reason={reason or '-'}"
        )
        return True, ""

    async def _welcome_after_approve(
        self, event: Optional[AstrMessageEvent], group_openid: str, platform_id: str,
        member_openid: str, username: str,
    ) -> Optional[str]:
        """审批通过后发欢迎语（主动消息优先，推不上去就把文案交回调用方）。

        从 ``_cmd_review`` 里抽出来，让按钮路径也能复用同一套降级逻辑。

        Args:
            event: 来源事件，可为 None（按钮路径拿不到可回复的 msg_id，仅记日志）。

        Returns:
            主动消息推成功、欢迎语功能关闭、或文案为空时返回 None；
            主动消息推失败时返回欢迎语文案，**调用方必须把它附在自己的回复里发出去**
            —— 早期这里用 ``self._reply(event, ...)`` 兜底，但按钮事件的 event
            没有 msg_id，适配器会直接跳过发送，欢迎语就这么静默消失了。
        """
        if not bool(self._read_config_value(
            ("basic", "enable_welcome"), default=True, legacy_keys=("enable_welcome",)
        )):
            return None
        if not self._claim_join_welcome(group_openid, member_openid):
            # 成员真进群的 GROUP_MEMBER_ADD 已经先发过欢迎了，这里别再补一条
            self.logger.info(
                f"群 {group_openid[-6:]} 成员 "
                f"{member_registry_lib.short_id(member_openid)} 刚欢迎过，审批路径跳过欢迎语"
            )
            return None
        welcome = self._render(
            "msg_welcome_after_approve",
            username=username,
            group_name=self._welcome_group_name(group_openid),
            bot_name=platform_id,
            at=member_openid,
        )
        if await self._send_group_proactive(
            group_openid, welcome, platform_id, at_openid=member_openid
        ):
            self.logger.info(f"已向群 {group_openid[-6:]} 推送欢迎语")
            return None
        self.logger.warning(
            "主动推送欢迎语失败（主动消息受限或频控），改由调用方随回执一起发出"
        )
        text = str(welcome or "").strip()
        if not text:
            self._release_join_welcome(group_openid, member_openid)
            return None
        # 这段文案马上就要由调用方发出去（回执/指令回复），槽位保持占用：
        # 否则成员真正进群触发 GROUP_MEMBER_ADD 时会再欢迎一遍。
        return text

    # ------------------------------------------------------------------
    # 入群申请轮询
    # ------------------------------------------------------------------
    def _managed_groups(self) -> dict[str, str]:
        """返回 {group_openid: platform_id} 轮询目标。"""
        if self._target_groups():
            out = {}
            for g, pid in self._known_groups.items():
                if self._target_group_allowed(g, pid):
                    out[g] = pid
                else:
                    self.logger.debug(f"群 {g} 不在管理名单，跳过轮询")
            return out
        return dict(self._known_groups)

    async def _poll_loop(self) -> None:
        mode = self._join_event_mode()
        self.logger.info(
            "入群申请轮询任务启动"
            f"（模式={mode}：event_first=事件为主+低频兜底，interval=纯轮询，off=关闭）"
        )
        while True:
            mode = self._join_event_mode()
            try:
                if mode == "off":
                    # 关闭轮询：只保留事件通道。申请仍可通过 /群管 审批 指令即时拉取。
                    await asyncio.sleep(300)
                    continue
                groups = self._managed_groups()
                if not groups:
                    self.logger.debug("轮询: 暂无可管理群，跳过")
                for g, pid in groups.items():
                    try:
                        await self._poll_group(g, pid)
                    except Exception as e:
                        self.logger.error(f"轮询群 {g} 失败: {e}")
            except Exception as e:
                self.logger.error(f"轮询循环异常: {e}")
            await asyncio.sleep(self._poll_interval_seconds())

    def _poll_interval_seconds(self) -> int:
        """本轮轮询间隔（秒）。

        - ``interval``：用户配的 ``poll_interval``（老行为，默认 30s）；
        - ``event_first``：读 ``poll_interval_fallback``（默认 300s），下限 60s。
          事件通道已经把「即时性」接走了，轮询只负责「事件漏推时补一刀」，
          没必要继续 30 秒一次去撞 30 QPM 的接口频控。
        """
        mode = self._join_event_mode()
        if mode == "event_first":
            raw = self._read_config_value(
                ("approval", "poll_interval_fallback"), default=300,
                legacy_keys=("poll_interval_fallback",),
            )
            try:
                return max(60, int(raw))
            except (TypeError, ValueError):
                return 300
        raw = self._read_config_value(
            ("approval", "poll_interval"), default=30, legacy_keys=("poll_interval",)
        )
        try:
            return max(10, int(raw))
        except (TypeError, ValueError):
            return 30

    async def _poll_group(self, group_openid: str, platform_id: str) -> None:
        # 用轮询任务自己维护的 _seen_requests 判断新申请，而不是拿共享的
        # self._pending 对比（它会被 /审批 指令、WebUI /requests 接口更新，
        # 导致申请被提前"见过"、通知永远发不出去）。
        seen = self._seen_requests.get(group_openid) or set()
        if time.time() < self._join_backoff.get(group_openid, 0.0):
            return
        items = await self._refresh_requests(group_openid, platform_id)
        if self._join_error_code.get(group_openid) == 11703:
            # 机器人不是群管理员：退避 30 分钟，且只提示一次，不再刷日志。
            self._join_backoff[group_openid] = time.time() + 1800
            if group_openid not in self._join_denied_logged:
                self._join_denied_logged.add(group_openid)
                self.logger.warning(
                    f"群 {group_openid} 机器人不是群管理员，入群申请通知不可用；"
                    "已改为每 30 分钟重试一次，重试成功会自动恢复"
                )
            return
        self._join_backoff.pop(group_openid, None)
        self._join_denied_logged.discard(group_openid)
        new = [r for r in items if self._req_key(r) not in seen]
        self._seen_requests[group_openid] = {self._req_key(r) for r in items}
        if not new:
            return
        self.logger.info(f"群 {group_openid} 轮询发现 {len(new)} 个入群申请")
        await self._notify_join_requests(group_openid, platform_id, new, source="轮询")

    # ------------------------------------------------------------------
    # 入群申请 · 事件通道（1.4.0 起的主通道：GROUP_JOIN_REQUEST）
    # ------------------------------------------------------------------
    # 设计取舍：
    # - 事件是**推送**：申请一提交就到，不用等下一轮轮询；平台只在机器人是
    #   群管理员时才推（官方前置条件）。
    # - 事件体比 join_request_list 的单条更全（多 username / verify_info /
    #   auto_approved），所以事件到了就直接用事件体，不额外打接口。
    # - 事件不能当被动消息回复（event_id 白名单只有 INTERACTION_CREATE /
    #   GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE），通知仍然走主动消息。
    # - 轮询不删，降级成兜底（event_first 模式，默认 5 分钟一次）。
    JOIN_NOTIFY_DEDUPE_SECONDS = 600  # 同一条申请 10 分钟内只推一次

    def _join_event_mode(self) -> str:
        mode = str(self._read_config_value(
            ("approval", "join_event_mode"), default="event_first",
            legacy_keys=("join_event_mode",),
        ) or "event_first").strip().lower()
        return mode if mode in ("event_first", "interval", "off") else "event_first"

    def _join_notify_enabled(self) -> bool:
        return bool(self._read_config_value(
            ("approval", "enable_join_request_notify"), default=True,
            legacy_keys=("enable_join_request_notify",),
        ))

    def _join_button_options(self) -> tuple[bool, str, int]:
        """(是否带按钮, 按钮权限 admin|everyone, 每条通知最多几个申请)。"""
        enabled = bool(self._read_config_value(
            ("approval", "notify_buttons"), default=True, legacy_keys=("notify_buttons",),
        ))
        perm = str(self._read_config_value(
            ("approval", "button_permission"), default="admin",
            legacy_keys=("button_permission",),
        ) or "admin").strip().lower()
        if perm not in ("admin", "everyone"):
            perm = "admin"
        try:
            limit = int(self._read_config_value(
                ("approval", "button_max_requests"), default=3,
                legacy_keys=("button_max_requests",),
            ) or 3)
        except (TypeError, ValueError):
            limit = 3
        return enabled, perm, max(1, min(limit, button_kit.MAX_ROWS))

    def _upsert_pending(self, group_openid: str, req: dict) -> bool:
        """把一条申请并入待审批缓存，返回它是否是**新**申请。

        去重键与轮询一致（member_openid + apply_at）。事件体字段比轮询结果全，
        所以命中已有条目时用事件体覆盖（``username`` / ``verify_info`` 以事件为准）。
        """
        pending = self._pending.setdefault(group_openid, [])
        key = self._req_key(req)
        for idx, old in enumerate(pending):
            if self._req_key(old) == key:
                pending[idx] = {**old, **req}
                return False
        pending.append(req)
        if len(pending) > 50:
            del pending[:-50]
        return True

    async def _on_group_join_request(self, data: dict, platform_id: str | None = None) -> None:
        """GROUP_JOIN_REQUEST 事件入口（由 member_event 注入的 handler 调过来）。"""
        if not isinstance(data, dict):
            return
        group_openid = str(data.get("group_openid") or "")
        member_openid = str(data.get("member_openid") or "")
        if not group_openid or not member_openid:
            self.logger.debug(f"[入群申请] 事件字段不完整，忽略: {list(data)[:6]}")
            return
        if not self._group_in_scope(group_openid, platform_id):
            self.logger.debug(f"[入群申请] 群 {group_openid[-6:]} 不在管理范围，忽略")
            return

        now = time.time()
        self._join_event_at[group_openid] = now
        self._join_event_last = now
        self._join_event_total += 1

        apply_at = str(data.get("apply_at") or "")
        req = {
            "join_request_id": str(data.get("join_request_id") or ""),
            "member_openid": member_openid,
            "username": str(data.get("username") or ""),
            "apply_at": apply_at,
            "apply_source": str(data.get("apply_source") or ""),
            "invited_by": str(data.get("invited_by") or ""),
            "risk_tips": str(data.get("risk_tips") or ""),
            "verify_info": data.get("verify_info") or {},
            "bot": bool(data.get("bot")),
            "_from": "event",
        }
        is_new = self._upsert_pending(group_openid, req)
        if req["username"]:
            try:
                self._nick_cache.setdefault(group_openid, {})[member_openid] = req["username"]
            except Exception:
                pass

        # 自动审批策略已经放行：这不是「待审批」，只发一条结果提示（可关）。
        auto = data.get("auto_approved") if isinstance(data, dict) else None
        if isinstance(auto, dict) and auto.get("strategy_id"):
            strategy = str(auto.get("strategy_id"))
            self.logger.info(
                f"[入群申请] 群 {group_openid[-6:]} 成员 {member_openid[-6:]} 已被策略 {strategy} 自动通过"
            )
            # 自动通过 = 不需要人工处理，从待审批缓存里摘掉
            self._pending[group_openid] = [
                x for x in self._pending.get(group_openid, [])
                if self._req_key(x) != self._req_key(req)
            ]
            if bool(self._read_config_value(
                ("approval", "notify_auto_approved"), default=True,
                legacy_keys=("notify_auto_approved",),
            )):
                text = self._render(
                    "msg_join_request_auto_approved",
                    username=req["username"] or member_openid[-6:],
                    strategy=strategy,
                    group_name=group_openid,
                )
                if await self._send_group_proactive(
                    group_openid, text, platform_id or self._known_groups.get(group_openid, "")
                ):
                    self.logger.info(f"[入群申请] 已推送自动通过提示到群 {group_openid[-6:]}")
            return

        self.logger.info(
            f"[入群申请] 群 {group_openid[-6:]} 收到申请"
            f"（{'新申请' if is_new else '刷新已有申请'}）"
            f" member={member_openid[-6:]} source={req['apply_source'] or '?'}"
        )
        if not is_new:
            # 同一申请重复推送（断线重连补事件）：缓存已更新，通知去重交给 _notified。
            pass
        if not self._join_notify_enabled():
            return
        if is_new:
            # 进合并队列，2 秒内的多条申请一起发（见 _flush_join_notify）
            self._join_fresh.setdefault(group_openid, []).append(req)
            self._schedule_join_notify(group_openid, platform_id)
        else:
            # 重复推送（断线补事件）：缓存已刷新，通知窗口内不再重复发，
            # 但如果是「上一批还没发出」的新申请，仍然让它照常合并发送。
            if self._join_fresh.get(group_openid):
                self._schedule_join_notify(group_openid, platform_id)

    JOIN_NOTIFY_DEBOUNCE_SECONDS = 2.0

    def _schedule_join_notify(self, group_openid: str, platform_id: str | None) -> None:
        """安排一次「合并推送」。已有待发任务时直接复用，不再叠加定时器。"""
        task = self._join_notify_tasks.get(group_openid)
        if task is not None and not task.done():
            return
        try:
            self._join_notify_tasks[group_openid] = asyncio.create_task(
                self._flush_join_notify(group_openid, platform_id)
            )
        except RuntimeError as e:
            # 没有事件循环（理论上不会发生，事件回调本身就在循环里）。
            # 批次会留在 _join_fresh 里，下一条事件到达时会再次触发合并推送。
            self.logger.warning(f"[入群申请] 无法创建合并任务（{e}），等下次事件重试")

    async def _flush_join_notify(self, group_openid: str, platform_id: str | None) -> None:
        """等一小会儿把同批申请合并成一条通知发出。"""
        try:
            await asyncio.sleep(self.JOIN_NOTIFY_DEBOUNCE_SECONDS)
            batch = self._join_fresh.pop(group_openid, [])
            if not batch:
                return
            if not self._join_notify_enabled():
                self.logger.debug("[入群申请] 通知已关闭，合并批次丢弃")
                return
            self.logger.debug(f"[入群申请] 合并推送 {len(batch)} 条申请")
            await self._notify_join_requests(
                group_openid, platform_id, batch, source="事件"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(f"[入群申请] 合并推送失败: {e}")
        finally:
            self._join_notify_tasks.pop(group_openid, None)

    async def _notify_join_requests(
        self, group_openid: str, platform_id: str | None, reqs: list[dict],
        *, source: str = "轮询",
    ) -> int:
        """把一批新申请推送到群里（事件与轮询共用），返回实际推送的条数。

        统一出口的好处：按钮、去重、失败降级只有一份实现——
        之前事件/轮询各写一套的话，按钮很容易只在其中一条路径上生效。
        """
        if not reqs:
            return 0
        api = self._api_for_group_or_default(group_openid, platform_id or "")
        if api is None:
            self.logger.warning(
                f"[入群申请] 群 {group_openid[-6:]} 找不到可用 API 客户端，通知跳过"
            )
            return 0

        now = time.time()
        dedupe_sec = self.JOIN_NOTIFY_DEDUPE_SECONDS
        notified = self._notified.setdefault(group_openid, {})
        fresh = [
            r for r in reqs
            if now - notified.get(self._req_key(r), 0) >= dedupe_sec
        ]
        if not fresh:
            self.logger.debug(f"[入群申请] {source}通知：{len(reqs)} 条申请近期已通知过，跳过")
            return 0

        pending = self._pending.get(group_openid) or []
        lines = []
        for r in fresh:
            try:
                idx = next(
                    i + 1 for i, x in enumerate(pending) if self._req_key(x) == self._req_key(r)
                )
            except StopIteration:
                idx = len(pending)
            lines.append(self._fmt_request_line(idx, r))
        text = self._render("msg_join_request_notify", list="\n".join(lines))

        buttons_on, perm, limit = self._join_button_options()
        keyboard = None
        if buttons_on and self._button_channel_ready():
            keyboard = button_kit.join_request_keyboard(
                fresh, permission=perm, max_requests=limit
            )
            if len(fresh) > limit:
                self.logger.info(
                    f"[入群申请] {len(fresh)} 条新申请，按钮只带前 {limit} 条"
                    f"（行数上限 {button_kit.MAX_ROWS}），其余请用 /群管 审批 处理"
                )
        elif buttons_on:
            self.logger.info("[入群申请] 当前 AstrBot 版本不支持按钮回调，转为纯文本通知")

        ok = await self._send_group_proactive(
            group_openid, text, platform_id or "", keyboard=keyboard
        )
        if not ok:
            self.logger.warning(
                f"[入群申请] 群 {group_openid[-6:]} 推送申请通知失败"
                f"（QQ 主动消息可能受权限/频控限制，可用 /群管 审批 手动查看）"
            )
            return 0
        for r in fresh:
            notified[self._req_key(r)] = now
        self.logger.info(
            f"[入群申请] 已推送 {len(fresh)} 条申请通知到群 {group_openid[-6:]}"
            f"（来源={source}，按钮={button_kit.describe_keyboard(keyboard) if keyboard else '无'}）"
        )
        return len(fresh)

    # ------------------------------------------------------------------
    # 消息按钮回调（INTERACTION_CREATE → AstrBot 的按钮事件）
    # ------------------------------------------------------------------
    # 链路（这条链路有平台差异，写清楚免得以后踩坑）：
    #   用户点按钮 → 平台推 INTERACTION_CREATE（intent 1<<26，botpy 自带 parser）
    #   → AstrBot 的 qqofficial 适配器 _handle_interaction_create 里
    #     **先 PUT /interactions/{id} 回 code=0**，再把点击转成一条普通
    #     AstrBotMessageEvent（消息链里是一个 ButtonInteraction 组件，
    #     message_str 为空，sender=group_member_openid，group_id=group_openid）
    #   → 插件用专门的 on_button_action（@filter.button_action）认领：
    #     唤醒阶段只放行挂了 ButtonActionFilter 的 handler，普通消息 handler
    #     根本收不到按钮事件。
    #
    # 两个由此推出的结论：
    # 1. 插件**不能**再自己 PUT 一次（官方「同一 interaction_id 只能回应一次」，
    #    重复回应会报错），所以操作失败/无权限的反馈只能靠回一条消息。
    # 2. 回执要用 event_id=<互动事件 id> 直连 API：适配器处理按钮点击时没有
    #    登记 msg_id，event.send() 走 send_by_session 会因「没有 msg_id」被跳过。
    def _button_channel_ready(self) -> bool:
        """当前 AstrBot 是否既能把点击转成事件、又肯把它交给本插件。

        两个条件缺一不可：
        1. ``AstrMessageEvent.get_button_interaction``（事件里带得出点击对象）；
        2. ``filter.button_action``（能注册带 ButtonActionFilter 的 handler）。
        少了第 2 条，点击事件会在唤醒阶段被丢掉，插件永远收不到 —— 这就是
        开发期首版「点了按钮只变灰、服务端啥也没发生」的原因。
        """
        if not _BUTTON_ACTION_SUPPORTED:
            return False
        return callable(getattr(AstrMessageEvent, "get_button_interaction", None))

    def _button_interaction_of(self, event: AstrMessageEvent):
        """取出事件里的按钮点击对象；不是按钮事件就返回 None。"""
        getter = getattr(event, "get_button_interaction", None)
        if callable(getter):
            try:
                bi = getter()
            except Exception:
                bi = None
            if bi is not None:
                return bi
        # 兼容路径：没有 get_button_interaction 的版本，用鸭子类型在消息链里找
        try:
            comps = event.get_messages() or []
        except Exception:
            comps = []
        for comp in comps:
            if hasattr(comp, "interaction_id") and hasattr(comp, "action_id"):
                return comp
        return None

    def _button_operator_allowed(self, operator: str, perm: str) -> bool:
        """按钮操作者权限复核（平台一道闸 + 插件一道闸）。

        - 按钮 ``permission.type=1``（perm=admin）时，客户端只让群主/管理员
          点得动 —— 这是第一道闸。官方群消息里拿不到 ``member_role``，
          插件无法自己验群角色，所以这种情况下信任平台。
        - ``admin_openids`` 名单命中直接放行（插件自定义管理员可能不是
          QQ 群管理员，靠名单补上）。
        - ``perm=everyone`` 时**绝不**信任平台：必须名单命中，
          否则任何群成员点一下就能放人进群。
        """
        extra = self._read_config_value(
            ("admin", "admin_openids"), default=[], legacy_keys=("admin_openids",)
        ) or []
        if operator and operator in [str(x) for x in extra]:
            return True
        return perm == "admin"

    async def _reply_button_result(
        self, event: Optional[AstrMessageEvent], group_openid: str,
        platform_id: str, text: str,
    ) -> None:
        """按钮操作的回执：优先 passive（event_id=互动事件 id），再退主动消息/普通回复。"""
        content = str(text or "").strip()
        if not content:
            return
        bi = self._button_interaction_of(event) if event is not None else None
        iid = str(getattr(bi, "interaction_id", "") or "") if bi is not None else ""
        api = self._api_for_group_or_default(group_openid, platform_id or "") if group_openid else None
        if api is not None and iid:
            try:
                await api.send_group_message(group_openid, content, event_id=iid)
                return
            except QQApiError as e:
                self.logger.warning(
                    f"[按钮] 被动回执失败（event_id={iid[:8]}…），改用普通回复: {e}"
                )
        if api is not None and group_openid:
            if await self._send_group_proactive(group_openid, text, platform_id or ""):
                return
        if event is not None:
            await self._reply(event, text)

    async def _handle_button_interaction(self, event: AstrMessageEvent) -> bool:
        """处理消息按钮点击，返回 True 表示这条事件已被本插件消费。"""
        bi = self._button_interaction_of(event)
        if bi is None:
            return False
        data = str(getattr(bi, "data", "") or "")
        if not data.startswith(button_kit.CALLBACK_PREFIX):
            return False  # 其他插件/其他用途的按钮，不插手

        iid = str(getattr(bi, "interaction_id", "") or "")
        now = time.time()
        # 幂等：同一 interaction 只处理一次（用户连点会重复推事件）
        self._interaction_done = {
            k: v for k, v in self._interaction_done.items() if now - v < 3600
        }
        if iid and iid in self._interaction_done:
            self.logger.debug(f"[按钮] interaction {iid[:8]}… 重复点击，忽略")
            return True
        if iid:
            self._interaction_done[iid] = now
        self._interaction_total += 1
        self._interaction_last = now

        group_openid = str(event.get_group_id() or "")
        operator = str(event.get_sender_id() or "")
        platform_id = event.get_platform_id()
        action = button_kit.parse_join_action(data)
        if action is None or not group_openid:
            self._interaction_last_result = "数据无效"
            self.logger.warning(
                f"[按钮] 回调数据无法解析或不在群场景（可能被伪造）：{data[:60]}"
            )
            await self._reply_button_result(
                event, group_openid, platform_id,
                self._render("msg_join_request_button_gone"),
            )
            return True

        if not self._group_in_scope(group_openid, platform_id):
            # 通知只发在生效范围内的群；配置被改过之后仍可能点到旧消息上的按钮，
            # 这里也要挡一道，避免越权处理。
            self._interaction_last_result = "群不在生效范围"
            self.logger.warning(
                f"[按钮] 群 {group_openid[-6:]} 不在生效范围内，忽略本次点击"
            )
            return True

        _on, perm, _limit = self._join_button_options()
        if not self._button_operator_allowed(operator, perm):
            self._interaction_last_result = "无权限"
            self.logger.warning(
                f"[按钮] 操作者 {operator[-6:]} 不在管理员名单，拒绝其审批操作"
            )
            await self._reply_button_result(
                event, group_openid, platform_id,
                self._render("msg_join_request_button_denied"),
            )
            return True

        # 真值以本地待审批列表为准：button_data 是不可信输入，只当「意图」。
        key = (str(action["member_openid"]), str(action["apply_at"]))
        pending = self._pending.get(group_openid) or []
        target = next((x for x in pending if self._req_key(x) == key), None)
        if target is None:
            # 缓存里没有：可能刚被指令处理掉，也可能缓存丢了 —— 主动拉一次对账
            try:
                fresh = await self._refresh_requests(group_openid, platform_id)
            except Exception:
                fresh = []
            target = next(
                (x for x in (fresh or []) if self._req_key(x) == key), None
            )
        if target is None:
            self._interaction_last_result = "已处理/已失效"
            self.logger.info(
                f"[按钮] 申请已不在待审批列表（member={key[0][-6:]} apply_at={key[1]}），"
                f"回执「已处理或已失效」"
            )
            await self._reply_button_result(
                event, group_openid, platform_id,
                self._render("msg_join_request_button_gone"),
            )
            return True

        op = "approve" if action["op"] == button_kit.OP_APPROVE else "decline"
        username = str(target.get("username") or key[0][-6:])
        ok, err = await self._apply_join_decision(
            event, group_openid, platform_id, target, op, via="按钮"
        )
        if not ok:
            self._interaction_last_result = f"失败: {err}"
            await self._reply_button_result(
                event, group_openid, platform_id,
                self._render("msg_join_request_button_failed", username=username, error=err),
            )
            return True

        if op == "approve":
            self._interaction_last_result = f"通过 {username}"
            # 先试一次主动推送欢迎语；推不上去（@ 的成员还没真正进群、或主动消息
            # 受限）就把它和回执合并，用 passive（event_id=互动事件 id）一次发出：
            # 这条通道不吃主动消息配额，也不依赖 msg_id，可靠得多。
            text = self._render("msg_join_request_button_ok", username=username)
            welcome_left = await self._welcome_after_approve(
                event, group_openid, platform_id,
                str(target.get("member_openid") or ""), username,
            )
            if welcome_left:
                text = f"{text}\n\n{welcome_left}"
            await self._reply_button_result(
                event, group_openid, platform_id, text
            )
        else:
            self._interaction_last_result = f"拒绝 {username}"
            await self._reply_button_result(
                event, group_openid, platform_id,
                self._render("msg_join_request_button_rejected", username=username),
            )
        return True

    # ------------------------------------------------------------------
    # Web API（WebUI）
    # ------------------------------------------------------------------
    # ==================================================================
    # 群规审核（/举报 触发；平时不判定、不执行、不发言）
    # ==================================================================
    # 设计约束（用户明确要求）：
    # 1) 只有收到 /举报 才进入审核；普通消息默认只做零成本的关键词巡检
    #    （且该巡检由「每群独立」的隐藏开关 auto_scan 控制，默认关）；
    # 2) 回复一律走被动消息（_reply -> event.send），不用主动推送；
    # 3) 话术里的「根据{basis}」= 命中的判定维度：关键词/群规/语义分析/图片识别。

    # ---------------- 基础工具 ----------------
    def _extract_images(self, event: AstrMessageEvent) -> list[str]:
        """抽取消息里的图片地址（用于多模态审核）。

        三个来源依次兜底，任一条取到就不至于让「图片维度」静默失效：
        1. AstrBot 已解析的 Image 组件（url / file 都可能是 http 地址）；
        2. 原始 payload 里的 attachments —— 适配器版本不同、组件没生成时的兜底；
        3. 官方群消息把「文字 + 图片」拆成两条投递时，由调用方用
           `_nearby_images()` 从相邻缓存补齐（这里只负责单条消息）。
        """
        urls: list[str] = []

        def add(u: str) -> None:
            u = _norm_media_url(u)
            if u.startswith("http") and u not in urls:
                urls.append(u)

        try:
            for c in (getattr(event.message_obj, "message", None) or []):
                if "image" not in type(c).__name__.lower():
                    continue
                for attr in ("url", "file", "path"):
                    add(getattr(c, attr, ""))
        except Exception:
            pass

        try:  # 原始附件兜底（图片可能只在 attachments 里，组件里没有）
            raw = getattr(event.message_obj, "raw_message", None)
            for att in (getattr(raw, "attachments", None) or []):
                if isinstance(att, dict):
                    ct = str(att.get("content_type") or "").lower()
                    u = att.get("url") or ""
                else:
                    ct = str(getattr(att, "content_type", "") or "").lower()
                    u = getattr(att, "url", "") or ""
                if ct.startswith("image") or _looks_like_image_url(u):
                    add(u)
        except Exception:
            pass
        return urls[:4]

    def _mod_settings(self, group: str) -> dict:
        return self.mod.get_settings(group)

    # 判定记录里留存的原文上限（字）。旧版只留 200 字，复核时看不到全貌。
    _VERDICT_TEXT_CAP = 1000

    def _verdict_text(self, rep: dict) -> dict:
        """判定记录要留的「原始内容」：够人工二次确认，又不至于把 verdicts.json 撑爆。

        - text：原文前 1000 字
        - text_len / text_truncated：原文总长与是否被截断（复核时能看出有没有剪）
        - images：图片条数。官方图片 URL 是临时签名链接，几小时必失效，
          存进记录里也点不开，所以只记数量，不存 URL。
        """
        raw = str(rep.get("text") or "")
        cap = int(self._VERDICT_TEXT_CAP)
        return {
            "text": raw[:cap],
            "text_len": len(raw),
            "text_truncated": len(raw) > cap,
            "images": len(rep.get("images") or []),
        }

    # moderation 话术模板里的内置模块占位符：{at} / {avatar} / {avatar:N}
    _MOD_MODULE_RE = re.compile(r"\{at\}|\{avatar(?::\d+)?\}")

    def _mod_tpl(self, group: str, key: str, **kw) -> str:
        """话术渲染：本群覆盖 -> 面板全局话术 -> 插件配置 -> 内置默认。

        注意 moderation.render_tpl 会把「未提供的 {变量}」一并清空，
        所以内置模块占位符（{at} / {avatar}）必须先换成哨兵保护起来，
        渲染完再还原，否则会像写错变量一样被静默吃掉。
        """
        # 第三步「插件配置」以前是漏的：面板「消息模板」页保存 mod_* 时写的是
        # 插件配置（web_tpl_formats_save → _write_config_value），而这里只看
        # moderation 存储里的全局/群覆盖，于是「改了审核话术但群里没变」。
        store = getattr(self.mod, "templates", None)
        if not isinstance(store, dict):
            store = {}
        group_override = ((store.get("groups") or {}).get(group) or {}).get(key)
        global_override = (store.get("global") or {}).get(key)
        if group_override:
            tpl = group_override
        elif global_override:
            tpl = global_override
        else:
            tpl = ""
            if hasattr(self, "config"):
                tpl = self._read_config_value(
                    ("templates", key), default="", legacy_keys=(key,)
                )
            if not str(tpl or "").strip():
                # 回落到 moderation 自己的取法（含内置默认），保持可注入/可测试
                tpl = ((self.mod.get_templates(group) or {}).get(key)
                       or moderation.DEFAULT_TEMPLATES.get(key, ""))
        target = str(kw.pop("at", "") or "")
        guards: list[str] = []

        def _guard(m):
            guards.append(m.group(0))
            return f"\x00QQGM_MOD{len(guards) - 1}\x00"

        tpl = self._MOD_MODULE_RE.sub(_guard, str(tpl or ""))
        out = moderation.render_tpl(tpl, **kw)
        for i, g in enumerate(guards):
            out = out.replace(f"\x00QQGM_MOD{i}\x00", g)
        # 带上 key/target：_reply 据此查发送格式，{avatar} 才知道该取谁的头像
        return _TplText(out, key, target)

    # QQ 官方引用「合并转发」时，reply 原文不是转发正文，而是另一套摘要清单：
    #   === 消息 1 ===
    #   [消息内容] [群聊的聊天记录]
    #   [消息类型] 引用消息
    #   [关联消息]
    #   --- 第1条 ---
    #       [消息内容] 该不会4.2出了吧？
    #       [发送者] 曾某人
    # 而转发消息本身在缓存里是 [群聊的聊天记录] / === 消息 N === / [消息内容] ...。
    # 两套格式互不包含，所以「整串 in」永远匹配不上；必须抽 [消息内容] 行做交集。
    _QUOTE_ITEM_RE = re.compile(r"\[消息内容\]\s*(.+)")

    @classmethod
    def _clean_quote_summary(cls, text: str) -> str:
        """把引用摘要清洗成「被引用那一条」的正文。

        适配器渲染摘要时会把**嵌套引用**也铺进来，例如：

            === 消息 1 ===
            [消息内容]  <@bot>
            /群管 举报
            [消息类型] 引用消息
            [关联消息]
            --- 第1条 ---
                [消息内容] 转人机

        整段拿去匹配缓存，会命中更里层的那条（上例的「转人机」）——
        目标从「被举报的消息」漂到别人的旧消息上，发送者跟着错，上下文也围着错的人取。
        所以在这里截断到 [关联消息] 之前，再剥掉标签与 <@openid> 占位；
        没有嵌套标记（合并转发等）时原样返回，别破坏 `_quote_forward_items` 的解析。
        """
        raw = str(text or "")
        if not raw:
            return ""
        cut = len(raw)
        for mark in ("[关联消息]", "--- 第1条 ---"):
            i = raw.find(mark)
            if i != -1:
                cut = min(cut, i)
        if cut == len(raw):
            return raw                      # 没有嵌套：保持原样（可能是合并转发摘要）
        t = raw[:cut]
        t = re.sub(r"===\s*消息\s*\d+\s*===", " ", t)
        t = re.sub(r"\[消息(?:内容|类型)\]\s*(?:引用消息|合并转发|图片|视频|语音|文件)?", " ", t)
        t = re.sub(r"<@!?[0-9A-Za-z_\-]+>", " ", t)   # 官方 @ 占位符
        t = re.sub(r"^-+\s*第\s*\d+\s*条\s*-+$", " ", t, flags=re.M)
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()
    _FORWARD_NOISE = {
        "[群聊的聊天记录]", "群聊的聊天记录", "[转发消息]", "[合并转发]",
        "[聊天记录]", "聊天记录",
    }

    @classmethod
    def _quote_forward_items(cls, text: str) -> list[str]:
        """从引用摘要里抽出被转发消息的正文行（去重保序，剔除占位标题）。"""
        items: list[str] = []
        for line in (text or "").splitlines():
            m = cls._QUOTE_ITEM_RE.search(line)
            if not m:
                continue
            v = m.group(1).strip()
            if not v or v in cls._FORWARD_NOISE:
                continue
            if v not in items:
                items.append(v)
        return items

    def _find_recent_msg(self, group: str, text: str = "", ts: float = 0.0,
                         max_age: float = 3600.0) -> Optional[dict]:
        """在消息缓存里找被举报的那条（优先按原文，其次转发摘要交集，最后按时间）。

        与撤回用的 _match_recent_msg_id 区别：这里要拿到**整条记录**
        （发送者 openid、图片、真实消息 ID），且时间窗更宽（审核不受 2 分钟限制）。
        """
        try:
            lst = self._recent_group_msgs.get(group) or []
            now = time.time()
            qitems = self._quote_forward_items(text)
            bare = (text or "").strip()
            best, best_score = None, -1.0
            top: list[dict] = []          # 与最高分并列的候选，用来判定「是不是撞了高频短句」
            for r in lst:
                if now - float(r.get("ts") or 0) > max_age:
                    continue
                score = 0.0
                rt = (r.get("text") or "").strip()
                if bare and rt == bare:
                    score += 10.0          # 原文完全一致最强
                elif bare and rt and (bare in rt or rt in bare):
                    score += 4.0
                elif qitems and rt:
                    # 被引用的是合并转发：按「子消息正文行」在缓存正文里的命中比例打分，
                    # 全中 = 8 分（低于原文完全一致，高于普通包含，不会误伤普通消息）。
                    hit = sum(1 for it in qitems if it in rt)
                    if hit:
                        score += 4.0 + 4.0 * hit / len(qitems)
                if ts:
                    score += max(0.0, 3.0 - abs(float(r.get("ts") or 0) - ts))
                if score > best_score:
                    best, best_score = r, score
                    top = [r]
                elif score == best_score and score > 0:
                    top.append(r)
                    # 并列时必须取「最近」那条：缓存是旧→新遍历，
                    # 只写 score > best_score 会让同文消息永远命中最早的一条（发送者就错了）。
                    if float(r.get("ts") or 0) > float(best.get("ts") or 0):
                        best = r
            if best is not None:
                senders = {str(r.get("sender") or "") for r in top}
                if len(senders) > 1:
                    self.logger.warning(
                        f"[审核] 引用目标存在 {len(top)} 条同分候选（{len(senders)} 个发送者），"
                        f"已按最近时间取 id={best.get('id')}；若判错请让当事人重新引用一次"
                    )
                    best = dict(best)
                    best["match_ambiguous"] = True
                self.logger.debug(
                    f"审核引用命中缓存: items={qitems[:3]} score={best_score:.1f} "
                    f"id={best.get('id')} text_len={len(best.get('text') or '')} "
                    f"candidates={len(top)}"
                )
            return best if best_score > 0 else None
        except Exception:
            return None

    def _mod_context_lines(self, group: str, rep: dict, settings: dict) -> list[str]:
        """取被举报消息前后的上下文（keep_context 关闭时返回空）。"""
        if not settings.get("keep_context", True):
            return []
        try:
            n = max(0, int(settings.get("context_size") or 0))
            if n <= 0:
                return []
            lst = self._recent_group_msgs.get(group) or []
            anchor = float(rep.get("ts") or 0)
            idx = len(lst) - 1
            for i, r in enumerate(lst):
                if abs(float(r.get("ts") or 0) - anchor) < 0.5:
                    idx = i
                    break
            start = max(0, idx - n)
            out = []
            for r in lst[start:idx]:
                who = (r.get("sender_name") or r.get("sender") or "群成员")
                txt = (r.get("text") or "").strip()
                if not txt and r.get("images"):
                    txt = "[图片]"
                if txt:
                    out.append(f"{who}：{txt[:120]}")
            return out
        except Exception:
            return []

    def _nearby_images(self, group: str, sender: str, anchor_ts: float,
                       window: float = 3.0, limit: int = 4) -> list[str]:
        """从相邻缓存里补图片。

        官方群消息里「文字 + 图片」经常拆成两条投递：文本那条有正文、图片那条正文为空。
        举报按正文匹配到的是文本那条，图片得从同一发送者、前后几秒的缓存里捞回来——
        否则图片维度会被静默跳过（判定日志里只剩「模式=纯文本」）。
        """
        urls: list[str] = []
        if not anchor_ts:
            return urls
        for r in reversed(self._recent_group_msgs.get(group) or []):
            if sender and str(r.get("sender") or "") != str(sender):
                continue
            if abs(float(r.get("ts") or 0) - float(anchor_ts)) > window:
                continue
            for u in (r.get("images") or []):
                if u not in urls:
                    urls.append(u)
            if len(urls) >= limit:
                break
        return urls[:limit]

    def _latest_image_msg(self, group: str, max_age: float = 120.0) -> Optional[dict]:
        """最近一条带图片的缓存消息。

        引用的是**纯图片消息**时，引用摘要里没有任何正文，按正文匹配必定失败；
        这种情况按「最近的图片消息」兜底，总比直接把图片维度丢掉好。
        """
        now = time.time()
        for r in reversed(self._recent_group_msgs.get(group) or []):
            if now - float(r.get("ts") or 0) > max_age:
                continue
            if r.get("images"):
                return r
        return None

    def _quote_attachments_from_raw(self, raw) -> list[str]:
        """从引用事件原始 payload 的 msg_elements[0].attachments 里挖被引用图片。

        AstrBot 会把 quoted attachments 塞进 Reply.chain，但部分版本/部分客户端
        的引用事件只给文本摘要，这里直接再读一遍原始字段做兜底。
        字段可能是 dict 也可能是对象，两种都兼容。
        """
        out: list[str] = []
        try:
            elems = getattr(raw, "msg_elements", None) or []
            if not elems:
                return out
            e0 = elems[0]
            atts = e0.get("attachments") if isinstance(e0, dict) else getattr(e0, "attachments", None)
            for att in (atts or []):
                if isinstance(att, dict):
                    ct = str(att.get("content_type") or "").lower()
                    u = att.get("url") or ""
                else:
                    ct = str(getattr(att, "content_type", "") or "").lower()
                    u = getattr(att, "url", "") or ""
                if not u:
                    continue
                if ct.startswith("image") or _looks_like_image_url(u):
                    u = _norm_media_url(u)
                    if u not in out:
                        out.append(u)
        except Exception:
            pass
        return out[:4]

    def _mod_resolve_reported(self, event: AstrMessageEvent) -> dict:
        """解析「被举报的消息」。

        沿用插件里已验证的引用解析链路（与 /撤回 同源）：
        Reply 组件原文 -> botpy 原始 payload -> 消息缓存按原文匹配真实消息 ID。
        返回 dict：quoted=是否带了引用，resolved=是否已定位到发送者。
        """
        out = {
            "quoted": False, "resolved": False, "id": "", "text": "",
            "sender": "", "sender_name": "", "images": [], "ts": time.time(),
        }
        g = event.get_group_id()
        if not g:
            return out
        message_id = ""
        quoted_content = ""
        quoted_images = []
        try:
            from astrbot.api.message_components import Reply

            reply = next((c for c in (event.message_obj.message or []) if isinstance(c, Reply)), None)
            if reply is not None:
                out["quoted"] = True
                rid = getattr(reply, "id", None)
                if rid:
                    message_id = str(rid)
                quoted_content = self._clean_quote_summary(getattr(reply, "message_str", "") or "")
                # reply.chain 里的文本才是被引用原文；message_str 有时只剩占位标题。
                chain_parts = []
                for sub in (getattr(reply, "chain", None) or []):
                    if type(sub).__name__ == "Image":
                        u = _norm_media_url(getattr(sub, "url", "") or getattr(sub, "file", "") or "")
                        if u.startswith("http") and u not in quoted_images:
                            quoted_images.append(u)
                    else:
                        t = str(getattr(sub, "text", "") or "")
                        if t.strip():
                            chain_parts.append(t.strip())
                if chain_parts:
                    quoted_content = self._clean_quote_summary("\n".join(chain_parts))
        except Exception:
            pass
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw is not None:
                mid, mcontent = self._extract_quote_from_raw(raw)
                if mid:
                    message_id = message_id or str(mid)
                    out["quoted"] = True
                if mcontent and not quoted_content:
                    quoted_content = str(mcontent)
                    out["quoted"] = True
                # 引用图片：适配器没转成 Image 组件时，直接读原始 attachments
                for u in self._quote_attachments_from_raw(raw):
                    if u not in quoted_images:
                        quoted_images.append(u)
        except Exception:
            pass

        rec = None
        if quoted_content:
            rec = self._find_recent_msg(g, text=quoted_content)
        if rec is None and message_id:
            for r in (self._recent_group_msgs.get(g) or []):
                if str(r.get("id") or "") == message_id:
                    rec = r
                    break
        if rec is None and not quoted_content.strip():
            # 引用的是纯图片消息：摘要里没有任何正文可匹配，按最近的图片消息兜底
            rec = self._latest_image_msg(g)
            if rec is not None:
                self.logger.info(
                    "[审核] 引用的是纯图片消息，按最近缓存图片兜底："
                    f"id={rec.get('id')} ts={rec.get('ts')}"
                )
        if rec is None:
            # 兜底：被举报的是合并转发时，摘要里的 [消息内容] 行至少能拿去审核，
            # 别把「[消息类型] 引用消息」这类噪声丢给模型。
            items = self._quote_forward_items(quoted_content)
            out["text"] = "\n".join(items) if items else quoted_content
            out["images"] = quoted_images
            # 内容有、发送者没定位到（引用的是 10 分钟以前的消息）：
            # 不冒充已定位，但也不放弃审核——交给上层走「只判定不处罚」。
            if out["text"].strip() or out["images"]:
                out["unattributed"] = True
            self.logger.debug(
                f"审核引用未命中缓存: 摘要长度={len(quoted_content)} items={items[:3]} "
                f"unattributed={bool(out.get('unattributed'))}"
            )
            return out
        real_id = str(rec.get("id") or "")
        # QQ 官方引用事件只给 REFIDX 引用索引，撤回 API 认的是缓存里的真实消息 ID
        if real_id and (not message_id or message_id.startswith(("REFIDX", "TMP_"))):
            message_id = real_id
        # 命中缓存时优先用缓存里的正文：合并转发的缓存正文是完整展开版，
        # 比引用摘要（夹着 [消息类型] 等元信息）更适合做审核判定。
        images = list(rec.get("images") or []) or list(quoted_images)
        if not images:
            # 「文字 + 图片」被拆成两条投递时，图片在那条正文为空的相邻消息里，
            # 按同发送者 ±3s 补齐；补不上就只能走纯文本，别假装有图。
            images = self._nearby_images(g, str(rec.get("sender") or ""), float(rec.get("ts") or 0))
            if images:
                self.logger.info(
                    f"[审核] 被举报消息本身无图，按相邻缓存补齐 {len(images)} 张（同发送者 ±3s）"
                )
        out.update({
            "resolved": True,
            "match_ambiguous": bool(rec.get("match_ambiguous")),
            "id": message_id or real_id,
            "text": str(rec.get("text") or "") or quoted_content,
            "sender": str(rec.get("sender") or ""),
            "sender_name": str(rec.get("sender_name") or ""),
            "images": images[:4],
            "ts": float(rec.get("ts") or time.time()),
        })
        return out

    # ---------------- 模型选择（不选则用当前会话的对话模型） ----------------
    def _provider_display(self, pid: str) -> str:
        """provider id -> 展示名（找不到就原样返回，仅用于面板回显）。"""
        for opt in self._provider_options():
            if str(opt.get("id")) == str(pid):
                return str(opt.get("display") or pid)
        return str(pid or "")

    def _group_umo(self, group: str) -> str:
        """群 openid -> 会话 umo（形如 platform:GroupMessage:group），用于取「该群当前在用的对话模型」。"""
        if not group:
            return ""
        pid = self._group_bot(group)
        return f"{pid}:GroupMessage:{group}" if pid else ""

    async def _mod_provider_info(self, event: Optional[AstrMessageEvent] = None,
                                 vision: bool = False, group: str = "") -> dict:
        """解析本次判定用哪个模型：配置指定 > 群/会话当前对话模型 > 全局默认 > 首个可用对话模型。

        返回 {"id", "display", "source"}；source ∈ config/session/default/fallback/none。
        说明：面板试跑没有 event，以前只能随便挑一个 provider（可能挑到 embedding），
        现在用「群 umo」反查该群正在用的对话模型，做到和群里实际审核同款模型。
        """
        key = "vision_provider_id" if vision else "moderation_provider_id"
        pid = str(self._read_config_value(("moderation", key), default="",
                                          legacy_keys=(key,)) or "").strip()
        if pid:
            return {"id": pid, "source": "config", "display": self._provider_display(pid)}
        umo = event.unified_msg_origin if event is not None else self._group_umo(group)
        if umo:
            try:
                cur = str(await self.context.get_current_chat_provider_id(umo=umo) or "").strip()
                if cur:
                    return {"id": cur, "source": "session", "display": self._provider_display(cur)}
            except Exception as e:
                self.logger.warning(f"[审核] 取当前会话模型失败（umo={umo}），回退默认模型: {e}")
        try:
            dft = str(await self.context.get_current_chat_provider_id(umo="") or "").strip()
            if dft:
                return {"id": dft, "source": "default", "display": self._provider_display(dft)}
        except Exception as e:
            self.logger.warning(f"[审核] 取默认对话模型失败，回退首个可用模型: {e}")
        # 最后兜底：第一个「对话类」provider（跳过 embedding/rerank/stt/tts，避免拿到非对话模型）
        for opt in self._provider_options():
            t = str(opt.get("type") or "").lower()
            if any(k in t for k in ("embedding", "rerank", "stt", "tts", "speech")):
                continue
            return {"id": str(opt.get("id") or ""), "source": "fallback",
                    "display": str(opt.get("display") or "")}
        return {"id": "", "source": "none", "display": ""}

    async def _mod_provider_id(self, event: Optional[AstrMessageEvent] = None,
                               vision: bool = False, group: str = "") -> str:
        return str((await self._mod_provider_info(event=event, vision=vision, group=group)).get("id") or "")

    # ---------------- 四维度判定 ----------------
    # ------------------------------------------------------------------
    # 变体检测（谐音 / 形近 / 插符号绕过）
    # ------------------------------------------------------------------
    def _variant_sync(self, force: bool = False) -> None:
        """把词库字面量 + 代码内置正则里的字面片段喂给变体索引。

        索引按签名复用：词库没变时只做一次签名比对，不会重复建表。
        """
        if self._variant_words is None or force:
            words: list[tuple[str, tuple]] = []
            try:
                words.extend(self.lexicons.literal_words())
            except Exception as e:
                self.logger.warning(f"[变体] 读取词库字面量失败：{e}")
            try:
                seen: set[str] = set()
                for cat, pats in (moderation.DEFAULT_KEYWORDS or {}).items():
                    for pat in pats or []:
                        for frag in variant_lib.literals_from_pattern(str(pat)):
                            if frag in seen:
                                continue
                            seen.add(frag)
                            words.append((frag, (cat, frag, "builtin", "内置规则片段")))
            except Exception as e:
                self.logger.debug(f"[变体] 内置正则片段提取失败：{e}")
            self._variant_words = words
        self.variant.set_words(self._variant_words)

    @staticmethod
    def _mod_extra_variant_words(settings: dict) -> list[tuple[str, tuple]]:
        """本群自定义关键词里的字面片段（正则语法会被拆掉，只取字面部分）。"""
        out: list[tuple[str, tuple]] = []
        try:
            for cat, pats in (settings.get("keywords") or {}).items():
                c = lexicon_lib.normalize_category(str(cat))
                for p in (pats or []):
                    for frag in variant_lib.literals_from_pattern(str(p)):
                        out.append((frag, (c, frag, "group", "本群自定义")))
        except Exception:
            return []
        return out

    @staticmethod
    def _variant_method_name(method: str) -> str:
        return {"pinyin": "谐音改写", "confusable": "形近改写", "abbr": "拼音缩写"}.get(str(method), "变体写法")

    def _mod_variant_scan(self, group: str, rep: dict, settings: dict) -> Optional[dict]:
        """变体检测：返回疑似命中摘要（不含命中的具体词），没命中或未开启则 None。

        定位：这是**疑似**信号，不参与「是否违规」的判定，也不影响置信度，
        只用于「撤回 + 记录 + 进待办」。所以即使误报，代价也限于一条撤回，不会误禁言。
        """
        if not settings.get("variant_check", False):
            return None
        text = str(rep.get("text") or "")
        if not text.strip():
            return None
        try:
            self._variant_sync()
            hits = self.variant.scan(
                text,
                extra_words=self._mod_extra_variant_words(settings),
                allow=self.lexicons.allow_words(),
                limit=3,
                pinyin=True,
                confusable=bool(settings.get("variant_confusable", True)),
                abbr=bool(settings.get("variant_abbr", True)),
                min_tokens=int(settings.get("variant_min_chars") or 3),
                allow_fuzzy=bool(settings.get("variant_fuzzy", False)),
            )
        except Exception as e:
            self.logger.warning(f"[变体] 扫描异常，本条跳过变体层：{e}")
            return None
        if not hits:
            return None
        order = {"high": 3, "medium": 2, "low": 1}
        best = max(hits, key=lambda h: order.get(str(h.get("strength")), 0))
        # 说明：命中词条本身一律不进日志/记录/面板（沿用「不展示词条内容」的既有约定）
        return {
            "count": len(hits),
            "method": str(best.get("method") or ""),
            "strength": str(best.get("strength") or ""),
            "category": str(best.get("category") or ""),
            "source": str(best.get("source") or ""),
            "hint": self._variant_method_name(str(best.get("method") or "")),
        }

    async def _mod_variant_action(self, event: AstrMessageEvent, group: str, rep: dict,
                                  settings: dict, variant: dict, result: dict) -> None:
        """变体疑似命中后的动作：**只撤回 + 记录，绝不自动禁言**（惩罚性动作须人工复核）。"""
        result.setdefault("variant_recalled", False)
        result["variant_note"] = ""
        if not settings.get("variant_recall", False):
            result["variant_note"] = "本群未开启「疑似即撤回」，仅记录（可在面板举报设置里开启）"
            return
        if settings.get("dry_run", True):
            result["variant_note"] = "试运行模式，未实际撤回"
            return
        mid = str(rep.get("id") or "")
        if not mid:
            result["variant_note"] = "无法定位被举报消息，未撤回"
            return
        try:
            max_age = float(settings.get("recall_max_age_seconds", 110) or 0)
        except (TypeError, ValueError):
            max_age = 110.0
        age = time.time() - float(rep.get("ts") or 0)
        if max_age > 0 and age > max_age:
            result["variant_note"] = (f"消息已发出 {int(age)} 秒，超出官方 2 分钟撤回时限，未撤回"
                                      f"（可将本群 recall_max_age_seconds 设为 0 强制尝试）")
            return
        api = self._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
        if api is None:
            result["variant_note"] = "无可用 API 客户端，未撤回"
            return
        try:
            await api.recall_group_message(group, mid)
            result["variant_recalled"] = True
            self._add_recall_record(
                group_openid=group,
                platform_id=event.get_platform_id(),
                message_id=mid,
                content=str(rep.get("text") or ""),
                source="variant_auto",
                operator="",
                operator_name="变体检测",
            )
            self.logger.info(
                f"[审核][变体] 已按疑似变体撤回消息 {mid} (group={group}, method={variant.get('method')})"
            )
        except Exception as e:
            result["variant_note"] = _recall_hint(e)
            self.logger.warning(f"[审核][变体] 撤回失败：{result['variant_note']}")

    async def _mod_variant_strike(self, event: AstrMessageEvent, group: str, rep: dict,
                                  settings: dict, variant: dict, result: dict,
                                  rec: Optional[dict]) -> dict:
        """变体累计升级（strike）：窗口内同一人的疑似变体次数 → 提醒 / 自动禁言。

        设计边界（延续「变体只是疑似」的定位）：
        - L1 提醒默认开：零风险，只发一条**被动消息** @ 已登记管理员 + 待办高亮；
        - L2 禁言默认关：须同时满足「阈值 + 人工确认过 ≥1 次 + 冷却期外」，缺一不可；
        - 本群无人登记管理员 → 不发提醒（不刷群、也不消耗主动消息配额）。
        """
        info = {"count": 0, "confirmed": 0, "level": moderation.STRIKE_NONE,
                "notified": False, "muted": False, "note": ""}
        if not settings.get("variant_strike_enabled", True):
            return info
        target = str(rep.get("sender") or "")
        if not target:
            return info
        try:
            win = max(1, int(settings.get("variant_strike_window_days") or 7))
        except (TypeError, ValueError):
            win = 7
        summ = self.mod.variant_strike_summary(group, target, window_days=win)
        count = int(summ["count"])
        info["count"] = count
        info["confirmed"] = int(summ["confirmed"])
        try:
            thr = max(1, int(settings.get("variant_strike_threshold") or 3))
        except (TypeError, ValueError):
            thr = 3
        # 只在「首次达阈值」和「阈值整数倍」时提醒（3、6、9…）：既不漏，也不每来一条刷一次群
        crossed = count >= thr and (count == thr or count % thr == 0)
        if crossed:
            info["level"] = moderation.STRIKE_TODO

        # ---------------- L2：自动禁言（默认关） ----------------
        if bool(settings.get("variant_strike_mute", False)) and not settings.get("dry_run", True):
            try:
                mute_thr = max(1, int(settings.get("variant_strike_mute_threshold") or 5))
            except (TypeError, ValueError):
                mute_thr = 5
            try:
                cool = max(0, int(settings.get("variant_strike_cooldown_hours") or 6)) * 3600
            except (TypeError, ValueError):
                cool = 6 * 3600
            exempt = (target in [str(x) for x in (settings.get("whitelist") or [])]
                      or self._is_qq_admin_openid(group, target))
            if count >= mute_thr and not exempt:
                if bool(settings.get("variant_strike_require_confirm", True)) and not summ["confirmed"]:
                    info["note"] = "未自动禁言：需先有 1 次人工确认过的变体记录"
                elif cool and (time.time() - int(summ["last_muted"] or 0)) < cool:
                    info["note"] = f"未自动禁言：冷却中（{int(cool / 3600)} 小时内已处理过）"
                else:
                    try:
                        cap = int(settings.get("max_mute_seconds") or 86400)
                    except (TypeError, ValueError):
                        cap = 86400
                    try:
                        secs = int(settings.get("variant_strike_mute_seconds") or 600)
                    except (TypeError, ValueError):
                        secs = 600
                    secs = max(60, min(secs, cap))
                    api = self._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
                    if api is None:
                        info["note"] = "未自动禁言：无可用 API 客户端"
                    else:
                        expire_ts = time.time() + secs
                        try:
                            await api.set_mute(group, target,
                                               mute_expire_at=QQApi.format_rfc3339(expire_ts), op="add")
                            info["muted"] = True
                            info["level"] = moderation.STRIKE_MUTE
                            info["mute_seconds"] = secs
                            self._add_mute_record(
                                group_openid=group,
                                platform_id=event.get_platform_id(),
                                member_openid=target,
                                username=str(rep.get("sender_name") or ""),
                                reason=f"连续疑似变体 ×{count}（近 {win} 天）",
                                seconds=secs, expire_ts=expire_ts,
                                operator="", operator_name="变体累计升级",
                            )
                            self.logger.info(f"[审核][变体] 累计升级禁言：target={target} count={count} "
                                             f"{secs}s group={group}")
                        except Exception as e:
                            info["note"] = f"未自动禁言：{e}"
                            self.logger.warning(f"[审核][变体] 累计升级禁言失败：{e}")

        # ---------------- L1：被动消息 @ 已登记管理员 ----------------
        mentions = ""
        if info["muted"] or (crossed and bool(settings.get("variant_strike_notify", True))):
            admins = self.mod.get_admins(group)
            if admins:
                # 只 @ 前 10 个，避免消息过长被截断
                mentions = " ".join(f"<@{a['openid']}>" for a in admins[:10])
                info["notified"] = True
            else:
                info["note"] = info["note"] or "本群无人登记管理员，未发送 @ 提醒"
        method = self._variant_method_name(str(variant.get("method") or ""))
        if info["notified"]:
            if info["muted"]:
                text = self._mod_tpl(group, "mod_variant_strike_muted", mentions=mentions,
                                     user=f"<@{target}>", at=target, count=count, days=win,
                                     duration=fmt_duration(int(info.get("mute_seconds") or 0)))
            else:
                text = self._mod_tpl(group, "mod_variant_strike_notify", mentions=mentions,
                                     user=f"<@{target}>", at=target, count=count, days=win, method=method)
            await self._reply(event, text)
        if info["note"]:
            self.logger.info(f"[审核][变体] {info['note']}（group={group} target={target} count={count}）")

        # 回填到本条判定记录：让待办能显示「累计 ×N」并按档位排序
        if rec and rec.get("id"):
            self.mod.update_verdict(int(rec["id"]), variant_strike={
                "count": count,
                "level": info["level"],
                "window_days": win,
                "notified": bool(info["notified"]),
                "muted": bool(info["muted"]),
                "confirmed": int(summ["confirmed"]),
            })
        result["variant_strike_notified"] = bool(info["notified"])
        return info

    def _mod_keyword_hit(self, text: str, settings: dict) -> Optional[dict]:
        """关键词维度（零成本）：按「本群自定义关键词 → 词库 → 代码内置正则」取第一条命中。

        词库（公开词库/页面上传）走字面量多模式匹配，几万条也是毫秒级；
        代码内置的正则仍然兜底，覆盖「加我vx」「刷单」这类结构化套路。
        """
        try:
            hit = self.lexicons.hit(text, extra_keywords=settings.get("keywords") or {})
        except Exception as e:                                    # 词库坏了不能拖垮审核
            self.logger.warning(f"[词库] 命中检测异常，已跳过词库层: {e}")
            hit = None
        if hit:
            return hit
        return moderation.keyword_hit(text)

    @staticmethod
    def _lex_hit_hint(hit: dict) -> str:
        """把命中信息写成「不含词条本身」的说明。

        词条内容一律不入日志、不入判定记录、不入面板：只说命中在哪一类、来自哪个词库、
        词长多少。管理员要核对具体词，可在词典页导出词库文件后本地查看。
        """
        src = str(hit.get("source") or "")
        kind = "正则规则" if hit.get("is_regex") else f"{len(str(hit.get('pattern') or ''))} 字词"
        return f"命中关键词规则：{kind}" + (f"（{src}）" if src else "")

    def _rebuild_mod_few_shot(self) -> None:
        """提示词示例 = 内置几条 + 人工标注样本（持久化，重启不丢）。

        以前误判样本只追加到内存，进程一重启就没了，等于“教训白记”。
        现在从判定记录里回收 feedback 标注：误判 → violation=false，
        判对 → violation=true；各取最多 20 条、每条 80 字，防止提示词无限膨胀。
        """
        lines = [self._mod_few_shot_base]
        try:
            samples = self.mod.feedback_samples(limit=20)
        except Exception as e:                                  # 老数据/损坏文件都不能挡住启动
            self.logger.debug(f"[审核] 人工样本读取失败，仅用内置示例: {e}")
            samples = {}
        for item in samples.get("false_positive") or []:
            lines.append(f"- 「{item['text']}」→ violation=false（人工确认的误判，勿再判违规）" + chr(10))
        for item in samples.get("true_positive") or []:
            cat = item.get("category") or "other"
            sev = item.get("severity") or "medium"
            lines.append(f"- 「{item['text']}」→ violation=true, basis=semantic, "
                         f"category={cat}, severity={sev}（人工确认的违规，尺度参照此条）" + chr(10))
        self._mod_few_shot = "".join(lines)

    def _mod_review_fast(self, group: str, rep: dict, respect_switches: bool = True,
                         scene: str = "举报") -> Optional[dict]:
        """L0 关键词 + L1 缓存：零成本，命中直接出结果。

        respect_switches=False 时忽略「关键词层」开关（供 /审核 测试、页面试跑等诊断入口用）。
        命中时会写一条 INFO，说明「这次为什么没调模型」——否则日志里只剩「收到举报」，
        管理员无从判断是走了关键词层还是模型压根没被调用。
        """
        text = rep.get("text") or ""
        settings = self._mod_settings(group)
        hit = None
        if (not respect_switches) or settings.get("enable_keyword", True):
            hit = self._mod_keyword_hit(text, settings)
        if hit:
            cat = hit["category"]
            src = str(hit.get("source") or "")
            self.logger.info(
                f"[{scene}][零成本层] 关键词命中（类型={self.mod.get_cat_names(group).get(cat, cat)}"
                f"{'，来源=' + src if src else ''}），未调用模型"
            )
            return {
                "violation": True,
                "basis": moderation.DIM_KEYWORD,
                "category": cat,
                "severity": "high" if cat in ("ad", "illegal", "porn", "politics") else "medium",
                "confidence": 1.0,
                "reason": self._lex_hit_hint(hit),
                "evidence": text[:80],
                "source": src,
                "from_cache": False,
            }
        cached = self.mod.cache_get(text)
        if cached and cached.get("violation"):
            cached["from_cache"] = True
            self.logger.info(f"[{scene}][零成本层] 判定缓存命中，未调用模型")
            return cached
        return None

    async def _mod_review_llm(self, event: Optional[AstrMessageEvent], group: str, rep: dict,
                              context_lines: list[str], reporter_note: str = "",
                              pid_override: str = "", respect_switches: bool = True,
                              scene: str = "举报") -> Optional[dict]:
        """L2 语义 / L3 多模态：调模型拿 JSON 判定。返回 None 表示失败或该层已关闭。

        日志口径（管理员最常问的「到底调没调模型、调的哪个、模型说了啥」）：
        - INFO：调用前的 provider / 模式 / 提示词规模，调用后的耗时、token、返回长度、
                解析出的判定结论；每一处「没调模型」的提前返回都写明原因。
        - DEBUG：提示词预览与模型原始返回（可能含群聊原文，默认级别下不落盘）。
        """
        settings = self._mod_settings(group)
        if respect_switches and not settings.get("enable_model", True):
            self.logger.debug(f"[{scene}][模型] 模型层已关闭，不调用模型")
            return None  # 模型层已关闭：不发任何请求（省钱、降噪）
        # 次数熔断：当天模型判定次数达到上限后不再调模型（自动降级为仅关键词层）
        daily_limit = int(settings.get("daily_budget") or 0)
        if daily_limit > 0:
            used_today = int(self.mod.stats(group).get("today_llm_calls", 0) or 0)
            if used_today >= daily_limit:
                self.logger.warning(
                    f"[{scene}][模型] 群 {group} 已达当日模型判定次数上限 "
                    f"（今日 {used_today}/{daily_limit} 次），跳过模型判定"
                )
                return None

        images = list(rep.get("images") or [])
        if respect_switches and not settings.get("enable_vision", True):
            images = []  # 图片层关闭：图片不进提示词，只判文字
        if not images and not (rep.get("text") or "").strip():
            self.logger.debug(f"[{scene}][模型] 无可判定内容（文本为空且图片层关闭），跳过模型调用")
            return None  # 没有任何可判定内容（例如关闭图片层后只剩图片）

        # 并发闸门：同群已在排队/执行中的模型调用达到上限时，本条消息不再排队，
        # 直接退回「零成本层」的结果（关键词没命中就按未违规处理），宁可漏判也不拖垮队列。
        queue_limit = int(settings.get("llm_queue_limit") or 0)
        pending_now = self._mod_llm_pending.get(group, 0)
        if queue_limit > 0 and pending_now >= queue_limit:
            self.logger.info(
                f"[{scene}][模型] 同群模型调用排队已满（{pending_now}/{queue_limit}），"
                f"本条跳过模型层，仅按零成本层结果处理"
            )
            return None
        sysp = moderation.build_system_prompt(
            dim_names=self.mod.get_dim_names(group),
            cat_names=self.mod.get_cat_names(group),
            group_rules=self.mod.get_rules(group),
            few_shot=self._mod_few_shot,
            common_ban=str(self._read_config_value(("moderation", "common_ban"), default="", legacy_keys=("common_ban",)) or ""),
        )
        up = moderation.build_user_prompt(
            message_text=rep.get("text") or "",
            sender_name=rep.get("sender_name") or rep.get("sender") or "",
            context_lines=context_lines,
            reporter_note=reporter_note,
            has_image=bool(images),
        )

        def _usage_text(resp_obj) -> str:
            usage = getattr(resp_obj, "usage", None)
            if usage is None:
                return ""
            try:
                return (f" tokens入={usage.input_other}(缓存 {usage.input_cached})"
                        f" tokens出={usage.output}")
            except Exception:
                return ""

        async def _call(pid_used: str, with_images: list[str]):
            mode = f"多模态({len(with_images)} 张图)" if with_images else "纯文本"
            self.logger.info(
                f"[{scene}][模型] 调用模型 provider={pid_used} 模式={mode} "
                f"提示词 {len(sysp) + len(up)} 字（system {len(sysp)} / user {len(up)}）"
            )
            self.logger.debug(
                f"[{scene}][模型] 提示词预览 system={sysp[:200]!r} … user={up[:400]!r}"
            )
            t0 = time.perf_counter()
            sem = self._mod_llm_sem.get(group)
            if sem is None:
                sem = self._mod_llm_sem[group] = asyncio.Semaphore(1)
            # 先占位再取锁：pending 计的是「排队 + 执行中」，入口处的阈值判定才准
            self._mod_llm_pending[group] = self._mod_llm_pending.get(group, 0) + 1
            timeout_s = float(settings.get("llm_timeout") or 0)
            try:
                async with sem:
                    pending = self.context.llm_generate(
                        chat_provider_id=pid_used,
                        system_prompt=sysp,
                        prompt=up,
                        image_urls=with_images or None,
                        temperature=0.0,
                        max_tokens=320,
                    )
                    # 超时必须自己兜：provider 卡住会一直占着同群的锁，后面的消息全排不上
                    r = await (asyncio.wait_for(pending, timeout_s) if timeout_s > 0 else pending)
            except asyncio.TimeoutError:
                ms = (time.perf_counter() - t0) * 1000
                self.logger.warning(
                    f"[{scene}][模型] 调用超时 provider={pid_used} 模式={mode} "
                    f"耗时 {ms:.0f}ms（阈值 {timeout_s:.0f}s），本条按判定失败处理"
                )
                self._mod_last_llm = {"ts": int(time.time()), "ok": False, "ms": round(ms),
                                      "scene": scene, "provider": pid_used, "error": "timeout"}
                raise
            except Exception as e:
                ms = (time.perf_counter() - t0) * 1000
                self.logger.warning(
                    f"[{scene}][模型] 调用异常 provider={pid_used} 模式={mode} "
                    f"耗时 {ms:.0f}ms: {e}"
                )
                self._mod_last_llm = {"ts": int(time.time()), "ok": False, "ms": round(ms),
                                      "scene": scene, "provider": pid_used,
                                      "error": str(e)[:120]}
                raise
            finally:
                self._mod_llm_pending[group] = max(0, self._mod_llm_pending.get(group, 1) - 1)
            text = getattr(r, "completion_text", "") or ""
            self.logger.info(
                f"[{scene}][模型] 调用完成 provider={pid_used} 耗时 "
                f"{(time.perf_counter() - t0) * 1000:.0f}ms{_usage_text(r)} 返回 {len(text)} 字"
            )
            self.logger.debug(f"[{scene}][模型] 原始返回: {text[:800]!r}")
            self._mod_last_llm = {"ts": int(time.time()), "ok": True,
                                  "ms": round((time.perf_counter() - t0) * 1000),
                                  "scene": scene, "provider": pid_used}
            return r

        pid = pid_override or await self._mod_provider_id(event, vision=bool(images), group=group)
        if not pid:
            self.logger.warning(
                f"[{scene}][模型] 找不到可用的对话模型"
                f"（未指定「审核模型」且取不到默认 Provider），跳过模型调用"
            )
            return None
        resp = None
        used_images: list[str] = []
        if images:
            vpid = await self._mod_provider_id(event, vision=True, group=group) if event is not None else pid
            # 多模态模型不支持图片时会直接抛错（官方文档明确），
            # 这里兜底降级为纯文本判定，绝不因此中断审核。
            try:
                resp = await _call(vpid or pid, images)
                used_images = images
            except Exception as e:
                self.logger.warning(f"[{scene}][模型] 多模态判定失败，降级为纯文本: {e}")
        if resp is None:
            try:
                resp = await _call(pid, [])
            except Exception as e:
                self.logger.error(f"[{scene}][模型] 模型调用失败: {e}")
                return None

        raw = getattr(resp, "completion_text", "") or ""
        verdict = moderation.parse_json_loose(raw)
        if not verdict:
            self.logger.warning(f"[{scene}][模型] 模型未返回合法 JSON（{len(raw)} 字）: {raw[:200]!r}")
            return None

        # 归一化 + 回填模型信息（时长由代码决定，模型无权指定）
        verdict["violation"] = bool(verdict.get("violation"))
        basis = str(verdict.get("basis") or "").strip()
        if basis not in moderation.ALL_DIMS:
            basis = moderation.DIM_VISION if used_images else moderation.DIM_SEMANTIC
        if used_images and basis in (moderation.DIM_SEMANTIC, moderation.DIM_RULE):
            basis = moderation.DIM_VISION
        verdict["basis"] = basis
        try:
            verdict["confidence"] = float(verdict.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            verdict["confidence"] = 0.0
        verdict["model"] = pid
        verdict["from_cache"] = False
        try:
            dim_name = self.mod.get_dim_names(group).get(basis, basis)
            cat_name = self.mod.get_cat_names(group).get(str(verdict.get("category") or ""), verdict.get("category") or "-")
            self.logger.info(
                f"[{scene}][模型] 判定结论 违规={verdict['violation']} 依据={dim_name} 类型={cat_name} "
                f"严重度={verdict.get('severity') or '-'} 置信度={verdict['confidence']:.2f} "
                f"理由={str(verdict.get('reason') or '')[:60]!r}"
            )
        except Exception as e:                                     # 仅日志，绝不因取名失败影响判定
            self.logger.debug(f"[{scene}][模型] 判定结论日志输出失败: {e}")
        return verdict

    # ---------------- 执行（撤回 + 禁言） ----------------
    def _mod_plan(self, group: str, verdict: dict, rep: dict, *,
                  settings: dict | None = None, prior_override=None) -> dict:
        """把「判定结果」变成「处罚计划」的唯一来源。

        试跑（_mod_predict_action）与真实执行（_mod_execute）都走这里，
        避免两份参数拼装漂移——之前就出现过「试跑显示警告、实际不处罚」。
        只计算，不产生任何副作用。
        """
        st = settings if settings is not None else self._mod_settings(group)
        smart = bool(st.get("smart_penalty", False))
        sev0 = str(verdict.get("severity") or "")
        plan = {
            "action": "none", "recall": False, "seconds": 0, "duration": "",
            "rule_name": "", "rule_id": "", "match_by": "none", "source": "none",
            "severity": sev0, "confidence": verdict.get("confidence", 0),
            "prior_offenses": 0, "escalation": 1.0, "escalation_capped": False,
            "smart": smart, "reason": "",
        }
        try:
            conf = float(verdict.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0

        prior = 0.0
        try:
            if prior_override is not None:
                prior = max(0.0, float(prior_override))
            elif smart or st.get("escalate_enabled", False):
                prior = float(self.mod.repeat_count(
                    group, str(rep.get("sender") or ""),
                    window_days=int(st.get("escalate_window_days") or 30),
                ))
        except Exception:
            prior = 0.0
        plan["prior_offenses"] = prior

        if not verdict or not verdict.get("violation"):
            plan["reason"] = "未违规"
            plan["trace"] = self._plan_trace(plan, st, conf)
            return plan

        thr = float(st.get("confidence_threshold") or 0.75)
        if conf < thr and not verdict.get("from_cache"):
            if smart and sev0.lower() == "high":
                plan.update(action="todo", reason=f"高危但置信度 {conf:.2f} < {thr:.2f}，转人工复核")
            else:
                plan.update(action="none", reason=f"置信度 {conf:.2f} < 阈值 {thr:.2f}")
            plan["trace"] = self._plan_trace(plan, st, conf)
            return plan

        dec = moderation.decide_action(
            verdict,
            rules=self.mod.get_rules(group),
            max_seconds=int(st.get("max_mute_seconds") or 604800),
            default_seconds=int(self._read_config_value(("moderation", "default_mute_seconds"), default=600) or 600),
            smart=smart,
            high_conf=thr,
            prior_offenses=prior,
            prior_bump_at=int(st.get("smart_tier_bump_at") or 2),
            force_mute_priors=int(st.get("smart_force_mute_priors") or 0),
        )
        kind = str(dec.get("action") or "mute")
        plan.update({
            "action": kind,
            "recall": bool(dec.get("recall")),
            "seconds": int(dec.get("seconds") or 0) if kind == "mute" else 0,
            "duration": fmt_duration(int(dec.get("seconds") or 0)) if kind == "mute" else "",
            "rule_name": dec.get("rule_name") or "",
            "rule_id": dec.get("rule_id") or "",
            "match_by": dec.get("match_by") or "none",
            "source": dec.get("source") or "legacy",
            "severity": dec.get("severity") or sev0,
            "reason": dec.get("skipped_reason") or "",
        })

        if kind == "mute" and prior:
            before = int(plan["seconds"] or 0)
            try:
                if smart and not st.get("escalate_enabled", False):
                    max_mult = float(st.get("smart_max_multiplier") or 3.0)
                    secs, mult = moderation.escalate_seconds(
                        before, prior, step=0.5, max_multiplier=max_mult,
                        cap=int(st.get("max_mute_seconds") or 604800),
                    )
                    intended = min(1.0 + float(prior) * 0.5, max_mult)
                else:
                    secs, mult = moderation.escalate_seconds(
                        before, prior,
                        step=float(st.get("escalate_step") or 1.0),
                        max_multiplier=float(st.get("escalate_max_multiplier") or 4.0),
                        cap=int(st.get("max_mute_seconds") or 604800),
                    )
                    intended = mult
                if secs != before:
                    plan["seconds"] = secs
                    plan["duration"] = fmt_duration(secs)
                    plan["escalation"] = mult
                elif intended > 1.0:
                    plan["escalation"] = intended
                    plan["escalation_capped"] = True
            except Exception as e:
                self.logger.warning(f"[审核] 累犯加刑计算失败，按原时长执行：{e}")

        plan["trace"] = self._plan_trace(plan, st, conf)
        return plan

    @staticmethod
    def _plan_trace(plan: dict, st: dict, conf: float) -> dict:
        """写进判定记录的决策依据，便于事后回答「为什么罚成这样」。"""
        return {
            "source": plan.get("source") or "none",
            "rule_id": plan.get("rule_id") or "",
            "rule_name": plan.get("rule_name") or "",
            "match_by": plan.get("match_by") or "none",
            "severity": plan.get("severity") or "",
            "confidence": round(float(conf or 0), 3),
            "threshold": float(st.get("confidence_threshold") or 0.75),
            "prior": plan.get("prior_offenses") or 0,
            "bump_at": int(st.get("smart_tier_bump_at") or 2),
            "force_at": int(st.get("smart_force_mute_priors") or 0),
            "escalation": plan.get("escalation") or 1.0,
            "capped": bool(plan.get("escalation_capped")),
            "smart": bool(st.get("smart_penalty", False)),
            "action": plan.get("action") or "none",
        }

    def _mod_predict_action(self, group: str, verdict: dict, rep: dict,
                            prior_override: int | None = None) -> dict:
        """试跑用：与真实执行共用 _mod_plan，只算不执行。"""
        return self._mod_plan(group, verdict, rep, prior_override=prior_override)

    async def _run_action(self, coro):
        """在动作闸门内执行一个 QQ API 调用。

        同时最多 2 个撤回/禁言请求；排队超过 5 秒就放弃本次动作（稍后可重试/人工处理），
        避免刷屏时几十条一起罚把接口打到限频、拖住事件循环。
        返回 (ok, error_message)。
        """
        if getattr(self, "_mod_action_sem", None) is None:
            self._mod_action_sem = asyncio.Semaphore(2)
        try:
            await asyncio.wait_for(self._mod_action_sem.acquire(), timeout=5)
        except asyncio.TimeoutError:
            try:
                coro.close()
            except Exception:
                pass
            return False, "动作队列繁忙（并发上限 2），本次未执行"
        try:
            await coro
            return True, ""
        finally:
            self._mod_action_sem.release()

    async def _mod_try_recall(self, event: AstrMessageEvent, group: str, rep: dict,
                              settings: dict) -> str:
        """尽力撤回被处罚消息，返回一行说明（失败不影响后续动作）。"""
        mid = str(rep.get("id") or "")
        if not mid:
            return ""
        try:
            max_age = float(settings.get("recall_max_age_seconds", 110) or 0)
        except (TypeError, ValueError):
            max_age = 110.0
        age = time.time() - float(rep.get("ts") or 0)
        if max_age > 0 and age > max_age:
            note = (f"消息已发出 {int(age)} 秒，超出官方 2 分钟撤回时限，跳过撤回"
                    f"（可将本群 recall_max_age_seconds 设为 0 强制尝试）")
            self.logger.info(f"[审核] {note}")
            return note
        api = self._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
        if api is None:
            return "无可用 API 客户端，跳过撤回"
        try:
            ok, err = await self._run_action(api.recall_group_message(group, mid))
            if not ok:
                return err
            self.logger.info(f"[审核] 已撤回消息 {mid} (group={group})")
            self._add_recall_record(
                group_openid=group,
                platform_id=event.get_platform_id(),
                message_id=mid,
                content=str(rep.get("text") or ""),
                source="report_auto",
                operator=event.get_sender_id(),
                operator_name=event.get_sender_name() or "",
            )
            return ""
        except Exception as e:
            note = _recall_hint(e)
            self.logger.warning(f"[审核] 撤回失败（不影响后续动作）: {note}")
            return note

    async def _send_mod_warning(self, event: AstrMessageEvent, group: str, rep: dict,
                                out: dict, dec: dict) -> None:
        """发一条警告（默认 markdown + @）。发送失败由 _reply 降级为纯文本。"""
        target = str(rep.get("sender") or "")
        sev_map = {"high": "严重", "medium": "中度", "low": "轻微"}
        try:
            secs = int(moderation.severity_to_seconds(str(out.get("severity") or "medium")))
            nxt = fmt_duration(secs)
        except Exception:
            nxt = "按群规处理"
        text = self._mod_tpl(
            group,
            "msg_mod_warn",
            at=target,
            rule=out.get("rule_name") or "群规",
            severity=sev_map.get(str(out.get("severity") or ""), "中度"),
            reason=out.get("reason") or "",
            next=nxt,
        )
        await self._reply(event, text)

    async def _mod_execute(self, event: AstrMessageEvent, group: str, rep: dict,
                           verdict: dict, settings: dict) -> dict:
        """按判定结果执行动作。返回执行摘要（含话术所需字段）。"""
        # 归因不可靠时一律只判定、不处罚：
        # - unattributed：引用超出缓存窗口，拿不到发送者
        # - match_ambiguous：有多条同文候选，选中哪条都可能是错的（禁错人的代价远大于漏判）
        forced_only = bool(rep.get("unattributed") or rep.get("match_ambiguous"))
        out = {
            "violation": bool(verdict.get("violation")),
            "basis": verdict.get("basis"),
            "category": verdict.get("category") or "other",
            "severity": str(verdict.get("severity") or ""),
            "confidence": verdict.get("confidence", 0),
            "reason": verdict.get("reason") or "",
            "rule_name": "",
            "seconds": 0,
            "duration": "",
            "executed": False,
            "skipped_reason": "",
            "recall_note": "",
            "dry_run": bool(settings.get("dry_run", True)) or forced_only,
            "unattributed": bool(rep.get("unattributed")),
            "ambiguous": bool(rep.get("match_ambiguous")),
            "cached": bool(verdict.get("from_cache")),
        }
        if not out["violation"]:
            return out
        plan = self._mod_plan(group, verdict, rep, settings=settings)
        out.update({
            "action": plan["action"],
            "rule_name": plan["rule_name"],
            "rule_id": plan["rule_id"],
            "match_by": plan["match_by"],
            "source": plan["source"],
            "severity": plan["severity"] or out["severity"],
            "seconds": plan["seconds"] if plan["action"] == "mute" else 0,
            "duration": plan["duration"] if plan["action"] == "mute" else "",
            "prior_offenses": plan["prior_offenses"],
            "escalation": plan["escalation"],
            "escalation_capped": bool(plan["escalation_capped"]),
            "policy_trace": plan.get("trace") or {},
        })
        kind = plan["action"]
        if kind == "none":
            out["skipped_reason"] = plan.get("reason") or "未触发处罚"
            return out

        target = str(rep.get("sender") or "")
        if not target:
            out["skipped_reason"] = "无法确定被举报消息的发送者（消息过久未在缓存中）"
            return out
        # 豁免：管理员 / 白名单成员
        wl = [str(x) for x in (settings.get("whitelist") or [])]
        if target in wl or self._is_qq_admin_openid(group, target):
            out["skipped_reason"] = "该成员在豁免名单中"
            return out

        if out["dry_run"]:
            out["skipped_reason"] = "试运行模式，未实际执行"
            return out

        # 待办不需要任何 API：高危低置信时先收口，避免被下面「无 API」逻辑误判成执行失败
        if kind == "todo":
            out["pending_review"] = True
            out["executed"] = False
            out["skipped_reason"] = out.get("skipped_reason") or "转人工复核"
            self.logger.info(f"[审核] {group} 判定转人工待办（reason={out['skipped_reason']}）")
            return out

        # 1) 撤回：警告与禁言都可能用到；失败不影响后续动作。
        # 官方限制：非管理员只能撤自己的消息（40062003）；超过 2 分钟不可撤回（40064004）。
        if kind in ("mute", "warn") and (kind == "mute" or plan.get("recall")):
            out["recall_note"] = await self._mod_try_recall(event, group, rep, settings)

        if kind == "warn":
            # 警告靠消息通道，不需要 API 客户端；发完即收口
            out["executed"] = True
            await self._send_mod_warning(event, group, rep, out, plan)
            self.logger.info(
                f"[审核] 已警告 {target[-6:]}（group={group}） severity={out.get('severity')} conf={out.get('confidence')}"
            )
            return out

        api = self._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
        if api is None:
            out["skipped_reason"] = "无可用 API 客户端"
            return out

        # 2) 禁言
        expire_ts = time.time() + int(out["seconds"])
        try:
            ok, err = await self._run_action(
                api.set_mute(group, target, mute_expire_at=QQApi.format_rfc3339(expire_ts), op="add")
            )
            if not ok:
                out["executed"] = False
                out["skipped_reason"] = err
                return out
        except Exception as e:
            self.logger.error(f"[审核] 禁言失败: {e}")
            out["skipped_reason"] = f"禁言失败：{e}"
            return out
        out["executed"] = True
        try:
            self._add_mute_record(
                group_openid=group,
                platform_id=event.get_platform_id(),
                member_openid=target,
                username=str(rep.get("sender_name") or ""),
                reason=f"举报审核：{out['reason']}"[:120],
                seconds=int(out["seconds"]),
                expire_ts=expire_ts,
                operator=event.get_sender_id(),
                operator_name=f"举报人:{event.get_sender_name() or ''}",
            )
        except Exception as e:
            self.logger.debug(f"[审核] 写入禁言历史失败: {e}")
        return out

    def _is_qq_admin_openid(self, group: str, member: str) -> bool:
        try:
            return member in (getattr(self, "_qq_group_admins", None) or {}).get(group, set())
        except Exception:
            return False

    def _mod_group_allowed(self, group: str) -> bool:
        """审核相关的群指令同样遵守 basic.target_group_openids 管理名单。"""
        return self._target_group_allowed(group)

    async def _mod_auto_variant_only(self, event: AstrMessageEvent, group: str, rep: dict,
                                     settings: dict, variant: dict) -> None:
        """巡检命中变体、但关键词/模型层没判定违规：只撤回 + 记录 + 进待办。

        与 /举报 的区别：巡检没有举报人可以答复，所以这里**不在群里发言**
        （只有「累计升级」真达阈值时，才按既有规则 @ 已登记管理员），避免自动巡检刷屏。
        定位同前：疑似信号，绝不禁言（L2 自动禁言仍受 variant_strike_mute 默认关约束）。
        """
        result = {
            "violation": False, "executed": False,
            "basis": moderation.DIM_KEYWORD,
            "category": str(variant.get("category") or ""),
            "severity": str(variant.get("strength") or ""),
            "confidence": 0, "reason": "", "rule_name": "",
            "seconds": 0, "duration": 0,
            "skipped_reason": "巡检疑似变体（未判定违规）",
            "dry_run": bool(settings.get("dry_run")), "cached": False,
        }
        await self._mod_variant_action(event, group, rep, settings, variant, result)
        rec = self.mod.add_verdict({
            "group": group,
            "msg_id": str(rep.get("id") or ""),
            "reporter": "auto_scan",
            "target": rep.get("sender") or "",
            "target_name": rep.get("sender_name") or "",
            **self._verdict_text(rep),
            "violation": False,
            "basis": result["basis"],
            "category": result["category"],
            "severity": result["severity"],
            "confidence": 0,
            "reason": "",
            "rule_name": "",
            "action": "none",
            "ban_seconds": 0,
            "skipped_reason": result["skipped_reason"],
            "executed": False,
            "dry_run": result["dry_run"],
            "from_cache": False,
            "recall_note": "",
            "model": "variant",
            "variant_hit": True,
            "variant": {"method": variant["method"], "strength": variant["strength"],
                        "count": variant["count"], "source": variant["source"]},
            "variant_recalled": bool(result.get("variant_recalled")),
            "variant_note": result.get("variant_note", ""),
            "todo_state": "" if settings.get("todo_enabled", True) else "disabled",
        })
        await self._mod_variant_strike(event, group, rep, settings, variant, result, rec)

    async def _maybe_auto_scan(self, event: AstrMessageEvent) -> None:
        """隐藏开关：本群自动巡检（每群独立，互不影响，默认关）。

        成本控制：默认只跑零成本的「关键词维度」；需要 AI 巡检时，
        在插件页面把该群的 auto_scan_llm 打开（会按 daily_budget 熔断）。
        变体检测（variant_check，默认关）在这里与判定并行、互不影响：
        命中只是「疑似」，只撤回 + 记录 + 进待办，绝不禁言。
        整条链路完全由本群的 auto_scan 开关控制，关了就连变体层一起不跑。
        """
        try:
            g = event.get_group_id()
            if not g:
                return
            st = self._mod_settings(g)
            if not st.get("auto_scan"):
                return
            text = self._extract_text(event)
            images = self._extract_images(event)
            # 纯图片消息也要能进巡检：官方群消息常把「文字 + 图片」拆成两条，
            # 只按 text 判会把图片那条整条丢掉（图片维度等于没开）。
            if (not text and not images) or (not text and not st.get("enable_vision", True)):
                return
            rep = {
                "id": str(getattr(event.message_obj, "message_id", "") or ""),
                "text": text,
                "sender": event.get_sender_id() or "",
                "sender_name": event.get_sender_name() or "",
                "images": images,
                "ts": time.time(),
            }
            # 变体疑似信号：与「是否违规」的判定并行、互不影响（variant_check 默认关，关着就不扫）
            variant = self._mod_variant_scan(g, rep, st)
            if variant:
                self.logger.info(
                    f"[审核][变体] 巡检疑似命中：method={variant['method']} "
                    f"strength={variant['strength']} hits={variant['count']} group={g}"
                )
            verdict = self._mod_review_fast(g, rep, scene="巡检")
            if verdict is None and st.get("auto_scan_llm"):
                verdict = await self._mod_review_llm(
                    event, g, rep, self._mod_context_lines(g, rep, st), "（自动巡检）", scene="巡检"
                )
            if not verdict or not verdict.get("violation"):
                # 没判定违规但疑似变体：单独走「只撤回+记录」分支（不发言、不禁言）
                if variant:
                    await self._mod_auto_variant_only(event, g, rep, st, variant)
                return
            result = await self._mod_execute(event, g, rep, verdict, st)
            # 变体疑似：没被判定路径处理掉的话，按群配置决定是否撤回（只撤回，不禁言）
            if variant and not result["executed"]:
                await self._mod_variant_action(event, g, rep, st, variant, result)
            rec = self.mod.add_verdict({
                "group": g, "msg_id": rep["id"], "reporter": "auto_scan",
                "target": rep["sender"], "target_name": rep["sender_name"],
                **self._verdict_text(rep), "violation": result["violation"],
                "basis": result["basis"], "category": result["category"],
                "confidence": result["confidence"], "reason": result["reason"],
                "rule_name": result["rule_name"],
                "action": str(result.get("action") or ("mute" if result["executed"] else "none")),
                "pending_review": bool(result.get("pending_review")),
                "rule_id": result.get("rule_id") or "",
                "match_by": result.get("match_by") or "none",
                "policy_trace": result.get("policy_trace") or {},
                "ban_seconds": result["seconds"], "skipped_reason": result["skipped_reason"],
                "executed": result["executed"], "dry_run": result["dry_run"],
                "recall_note": result.get("recall_note", ""),
                "from_cache": result["cached"],
                "severity": result.get("severity") or verdict.get("severity") or "",
                "prior_offenses": result.get("prior_offenses") or 0,
                "escalation": result.get("escalation") or 1.0,
                "model": verdict.get("model") or "keyword",
                "variant_hit": bool(variant),
                "variant": ({"method": variant["method"], "strength": variant["strength"],
                             "count": variant["count"], "source": variant["source"]} if variant else None),
                "variant_recalled": bool(result.get("variant_recalled")),
                "variant_note": result.get("variant_note", ""),
                "todo_state": "" if st.get("todo_enabled", True) else "disabled",
            })
            if variant:
                await self._mod_variant_strike(event, g, rep, st, variant, result, rec)
            if result["executed"]:
                await self._reply(event, self._mod_tpl(
                    g, "mod_hit",
                    basis=self.mod.get_dim_names(g).get(str(result["basis"]), "多重维度"),
                    category=self.mod.get_cat_names(g).get(str(result["category"]), "违规"),
                    duration=result["duration"], user=rep["sender_name"], reason=result["reason"],
                ))
        except Exception as e:
            self.logger.debug(f"[审核] 自动巡检异常: {e}")

    # ---------------- /举报 ----------------
    async def _cmd_report_violation(
        self, event: AstrMessageEvent, parts: list[str]
    ) -> None:
        """/举报：引用违规消息 → 群规审核（原实现曾误命名为 _cmd_report，
        与「/群管 汇报」的日报方法重名，被后者覆盖导致 /汇报 直接 TypeError）。"""
        event.call_llm = True
        g = event.get_group_id()
        if not g:
            return
        if not self._mod_group_allowed(g):
            return
        settings = self._mod_settings(g)
        if not settings.get("enabled", True):
            await self._reply(event, self._mod_tpl(g, "mod_disabled"))
            return
        reporter = event.get_sender_id() or ""
        if not self.mod.report_allowed(g, reporter, int(settings.get("report_limit_per_hour") or 0)):
            await self._reply(event, self._mod_tpl(g, "mod_report_limit") or "举报太频繁啦，请稍后再试。")
            return
        rep = self._mod_resolve_reported(event)
        if not rep.get("quoted"):
            await self._reply(event, self._mod_tpl(g, "mod_no_quote"))
            return
        has_content = bool((rep.get("text") or "").strip() or rep.get("images"))
        if not has_content or (not rep.get("resolved") and not rep.get("unattributed")):
            # 引用了但既没内容、也无法定位目标：绝不猜人，直接如实提示重新引用
            await self._reply(event, self._mod_tpl(g, "mod_quote_lost"))
            self.mod.add_verdict({
                "group": g, "reporter": reporter, "target": "", "text": "",
                "violation": False, "error": "unresolved_quote",
                "dry_run": bool(settings.get("dry_run")),
                "skipped_reason": "引用消息过旧或无法定位",
            })
            return
        if rep.get("unattributed"):
            # 引用的是 10 分钟以前的消息：正文/图片拿得到，但发送者定位不到。
            # 仍然走一遍判定让人看到结果，但强制不处罚，也不做变体升级。
            self.logger.info(
                "[审核] 未定位到被举报消息的发送者（超出消息缓存窗口），"
                f"本次只判定不处罚: text_len={len(rep.get('text') or '')} images={len(rep.get('images') or [])}"
            )
        mid = str(rep.get("id") or "")
        if mid and mid in self._mod_pending:
            return  # 同一条消息正在审核，忽略重复举报
        if mid:
            self._mod_pending.add(mid)
        note = " ".join(parts[1:])[:200].strip() if len(parts) > 1 else ""
        try:
            self.logger.info(
                f"[审核] 收到举报: group={g} reporter={reporter} "
                f"text={rep.get('text', '')[:60]!r} images={len(rep.get('images') or [])} "
                f"resolved={bool(rep.get('resolved'))}"
            )
            # 变体检测（疑似信号，与「是否违规」的判定并行、互不影响）：
            # 命中只做 撤回 + 记录 + 进待办，绝不自动禁言。
            variant = None if rep.get("unattributed") else self._mod_variant_scan(g, rep, settings)
            if variant:
                self.logger.info(
                    f"[审核][变体] 疑似命中：method={variant['method']} strength={variant['strength']} "
                    f"hits={variant['count']} group={g}"
                )
            # 先跑零成本层：命中就一条消息直接出结果，不打扰群
            verdict = self._mod_review_fast(g, rep)
            if verdict is None and not settings.get("enable_model", True):
                # 模型层已关闭：不能只回「受理回执」然后没下文，如实说明只跑了关键词层
                await self._reply(event, self._mod_tpl(g, "mod_layer_off"))
                self.mod.add_verdict({
                    "group": g, "msg_id": mid, "reporter": reporter, "target": rep.get("sender"),
                    **self._verdict_text(rep), "violation": False,
                    "basis": moderation.DIM_KEYWORD, "category": "", "severity": "",
                    "confidence": 0, "error": "model_layer_off",
                    "skipped_reason": "模型层已关闭，仅跑关键词层（未命中）",
                    "dry_run": bool(settings.get("dry_run")),
                })
                return
            if verdict is None:
                await self._reply(event, self._mod_tpl(g, "mod_receiving"))
                verdict = await self._mod_review_llm(event, g, rep, self._mod_context_lines(g, rep, settings), note)
            if verdict is None:
                await self._reply(event, self._mod_tpl(g, "mod_failed", error="模型未返回结果"))
                self.mod.add_verdict({
                    "group": g, "msg_id": mid, "reporter": reporter, "target": rep.get("sender"),
                    "violation": False, "error": "llm_failed", "dry_run": bool(settings.get("dry_run")),
                })
                return

            result = await self._mod_execute(event, g, rep, verdict, settings)
            # 变体疑似：没被禁言路径处理掉的话，这里按群配置决定是否撤回（只撤回，不禁言）
            if variant and not result["executed"]:
                await self._mod_variant_action(event, g, rep, settings, variant, result)
            rec = self.mod.add_verdict({
                "group": g,
                "msg_id": mid,
                "reporter": reporter,
                "target": rep.get("sender"),
                "target_name": rep.get("sender_name"),
                **self._verdict_text(rep),
                "violation": result["violation"],
                "basis": result["basis"],
                "category": result["category"],
                "confidence": result["confidence"],
                "reason": result["reason"],
                "rule_name": result["rule_name"],
                "action": str(result.get("action") or ("mute" if result["executed"] else "none")),
                "pending_review": bool(result.get("pending_review")),
                "rule_id": result.get("rule_id") or "",
                "match_by": result.get("match_by") or "none",
                "policy_trace": result.get("policy_trace") or {},
                "ban_seconds": result["seconds"],
                "skipped_reason": result["skipped_reason"],
                "recall_note": result.get("recall_note", ""),
                "executed": result["executed"],
                "dry_run": result["dry_run"],
                "from_cache": result["cached"],
                "severity": result.get("severity") or verdict.get("severity") or "",
                "prior_offenses": result.get("prior_offenses") or 0,
                "escalation": result.get("escalation") or 1.0,
                # 变体检测：只记「方法/强度/条数」，不记命中的具体词（沿用不展示词条的约定）
                "variant_hit": bool(variant),
                "variant": ({"method": variant["method"], "strength": variant["strength"],
                             "count": variant["count"], "source": variant["source"]} if variant else None),
                "variant_recalled": bool(result.get("variant_recalled")),
                "variant_note": result.get("variant_note", ""),
                # 待办状态：本群关掉待办时直接标 disabled，不进队列（记录本身照常留痕）
                "todo_state": "" if settings.get("todo_enabled", True) else "disabled",
                "model": verdict.get("model") or "keyword",
            })
            # 变体累计升级：窗口内同一人疑似变体累计到阈值 → 提醒（默认）/ 自动禁言（默认关）
            if variant:
                await self._mod_variant_strike(event, g, rep, settings, variant, result, rec)
            # 只缓存「过了置信度阈值」或试运行下的违规判定：
            # 置信度不足被放过的判定绝不能进缓存，否则它会在 TTL 内绕过阈值直接执行
            if result["violation"] and "置信度" not in str(result["skipped_reason"]):
                self.mod.cache_put(rep.get("text") or "", verdict)
            # 回复话术（被动消息）
            if not result["violation"]:
                if variant:
                    if result.get("variant_strike_notified"):
                        return  # 累计提醒消息里已含变体说明，不再重复刷群
                    # 疑似变体：优先把这件事说清楚（撤回了还是只记录了，如实告知）
                    tpl = "mod_variant_recalled" if result.get("variant_recalled") else "mod_variant_logged"
                    await self._reply(event, self._mod_tpl(g, tpl, method=variant["hint"]))
                    return
                if settings.get("reply_when_clean", True):
                    await self._reply(event, self._mod_tpl(g, "mod_clean"))
                return
            if result["dry_run"]:
                if result.get("ambiguous"):
                    await self._reply(
                        event,
                        "⚠️ 这条内容被判为疑似违规，但缓存里有两条以上同文消息，"
                        "无法确定被举报的是哪一条，因此没有执行处罚。\n"
                        "请直接引用目标消息、尽快发一次 `/群管 举报`。",
                    )
                    return
                if result.get("unattributed"):
                    await self._reply(
                        event,
                        "⚠️ 这条内容被判为疑似违规，但引用超出可定位范围（超过 10 分钟），"
                        "无法确定发送者，因此没有执行处罚。\n"
                        "需要处理的话，请当事人重新引用原消息发一次 `/群管 举报`。",
                    )
                    return
                await self._reply(event, self._mod_tpl(
                    g, "mod_dry_run",
                    basis=self.mod.get_dim_names(g).get(str(result["basis"]), str(result["basis"] or "")),
                    category=self.mod.get_cat_names(g).get(str(result["category"]), str(result["category"] or "")),
                    duration=result["duration"],
                ))
                return
            if result.get("pending_review"):
                await self._reply(event, self._mod_tpl(g, "msg_mod_todo"))
                return
            if not result["executed"]:
                # 置信度不足 / 豁免成员 / 执行失败：宁可回复“未处理”也不误报“已禁言”
                rsn = str(result["skipped_reason"] or "")
                if "豁免" in rsn:
                    await self._reply(event, self._mod_tpl(g, "mod_exempt"))
                elif "置信度" in rsn:
                    if result.get("variant_recalled"):
                        await self._reply(event, self._mod_tpl(g, "mod_variant_recalled",
                                                               method=(variant or {}).get("hint", "变体写法")))
                    else:
                        await self._reply(event, self._mod_tpl(g, "mod_uncertain"))
                else:
                    await self._reply(event, self._mod_tpl(g, "mod_failed", error=rsn[:40] or "执行未成功"))
                return
            if str(result.get("action") or "") == "warn":
                return          # 警告已经公开发过，别再补一条“已禁言”
            await self._reply(event, self._mod_tpl(
                g, "mod_hit",
                basis=self.mod.get_dim_names(g).get(str(result["basis"]), "多重维度"),
                category=self.mod.get_cat_names(g).get(str(result["category"]), str(result["category"] or "违规")),
                duration=result["duration"],
                user=rep.get("sender_name") or "",
                reason=result["reason"],
            ))
        except Exception as e:
            self.logger.exception(f"[审核] 处理举报异常: {e}")
            await self._reply(event, self._mod_tpl(g, "mod_failed", error=str(e)[:60]))
        finally:
            if mid:
                self._mod_pending.discard(mid)

    # ---------------- /群规 ----------------
    async def _cmd_admin_register(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """审核提醒名单的管理员登记：/管理登记 [取消|列表]

        为什么要登记而不是自动识别：QQ 官方通道下群消息拿不到稳定的角色字段
        （_get_qq_role 恒为 None），而"谁在线、谁愿意兜底"本来就是群自己的事。
        所以由管理员主动发一条指令把自己写进本群名单，变体累计提醒只 @ 名单内的人；
        一个群没人登记，就不发提醒（不刷群、不浪费主动消息配额）。
        """
        event.call_llm = True
        g = event.get_group_id() or ""
        if not g:
            await self._reply(event, "本指令只能在群里使用")
            return
        if not self._mod_group_allowed(g):
            return
        sender = str(event.get_sender_id() or "")
        name = str(event.get_sender_name() or "")
        sub = parts[1].strip() if len(parts) > 1 else ""
        if sub in ("列表", "查看", "list"):
            admins = self.mod.get_admins(g)
            if not admins:
                await self._reply(event, self._mod_tpl(g, "mod_admin_list_empty"))
                return
            names = "、".join((a.get("name") or f"<@{a['openid']}>") for a in admins[:20])
            await self._reply(event, self._mod_tpl(g, "mod_admin_list",
                                                   count=len(admins), admins=names))
            return
        if sub in ("取消", "移除", "注销", "off"):
            ok = self.mod.unregister_admin(g, sender)
            cnt = len(self.mod.get_admins(g))
            key = "mod_admin_unregistered" if ok else "mod_admin_not_registered"
            await self._reply(event, self._mod_tpl(g, key, user=f"<@{sender}>", at=sender, count=cnt))
            return
        # 登记：能走到这里说明已经过了管理员门槛（AstrBot 管理员 / 配置名单 / 群角色任一）
        role = str(self._get_qq_role(event) or "")
        if not role and self._is_qq_admin_openid(g, sender):
            role = "qq_admin"
        if not role:
            role = "astrbot_admin"
        self.mod.register_admin(g, sender, name=name, role=role, by=sender)
        cnt = len(self.mod.get_admins(g))
        self.logger.info(f"[审核] 管理员登记：group={g} sender={sender} role={role} 名单共 {cnt} 人")
        await self._reply(event, self._mod_tpl(g, "mod_admin_registered",
                                               user=f"<@{sender}>", at=sender, count=cnt))
        if not self._mod_settings(g).get("variant_strike_notify", True):
            await self._reply(event, self._mod_tpl(g, "mod_admin_register_off"))

    async def _cmd_rules(self, event: AstrMessageEvent, parts: list[str]) -> None:
        event.call_llm = True
        g = event.get_group_id()
        if not g:
            return
        if not self._mod_group_allowed(g):
            return
        sub = parts[1] if len(parts) > 1 else ""
        rules = self.mod.get_rules(g)

        if not sub or sub in ("查看", "列表"):
            if not rules:
                await self._reply(event, self._mod_tpl(g, "mod_rules_empty"))
                return
            lines = [f"【本群群规】共 {len(rules)} 条"]
            for i, r in enumerate(rules, 1):
                flag = "" if r.get("enabled", True) else "（已停用）"
                lines.append(
                    f"{i}. {r.get('name')}{flag}｜{r.get('desc','')}"
                    f"｜{self.mod.get_cat_names(g).get(str(r.get('category')), r.get('category'))}"
                    f"｜{fmt_duration(int(r.get('seconds') or 0))}"
                )
            await self._reply(event, chr(10).join(lines))
            return

        # 以下为写操作，需要管理员
        if not self._is_admin(event):
            await self._reply(event, self._mod_tpl(g, "mod_need_admin"))
            return

        if sub in ("设置", "修改"):
            text = " ".join(parts[2:]).strip()
            if not text:
                await self._reply(event, RULE_USAGE_SET)
                return
            items = [x.strip() for x in re.split("[" + chr(10) + ";]", text) if x.strip()]
            new_rules, bad = [], []
            for it in items:
                name, desc, secs, err = parse_rule_spec(it)
                if err:
                    bad.append(it)
                    continue
                new_rules.append({
                    "name": name, "desc": desc or name,
                    "category": "other", "severity": "medium",
                    "seconds": int(secs or 3600), "enabled": True,
                })
            for r in new_rules:
                self.mod.add_rule(g, r)
            if not new_rules:
                await self._reply(event, RULE_USAGE_SET)
                return
            msg = f"已保存 {len(new_rules)} 条群规，当前共 {len(self.mod.get_rules(g))} 条。"
            if bad:
                msg += chr(10) + f"有 {len(bad)} 行没看懂，已跳过：" + " / ".join(bad[:3])
            await self._reply(event, msg)
            return

        if sub in ("添加", "新增", "add"):
            text = " ".join(parts[2:]).strip()
            name, desc, secs, err = parse_rule_spec(text)
            if err:
                tip = RULE_USAGE_ADD
                if err == "name_too_long":
                    tip = ("规则名太长了（超过 24 字），中间八成漏了竖线分隔。" + chr(10) + RULE_USAGE_ADD)
                await self._reply(event, tip)
                return
            rule = self.mod.add_rule(g, {
                "name": name, "desc": desc,
                "category": "other", "severity": "medium",
                "seconds": int(secs or 3600), "enabled": True,
            })
            # 回显完整内容：让用户当场看出说明和时长有没有被吃掉
            await self._reply(event, (
                f"已添加群规 {rule['id']}" + chr(10) +
                f"名称：{rule['name']}" + chr(10) +
                f"说明：{rule['desc'] or '（未填，按名称判定）'}" + chr(10) +
                f"判定命中后禁言：{fmt_duration(int(rule['seconds']))}"
            ))
            return

        if sub in ("删除", "移除"):
            key = parts[2] if len(parts) > 2 else ""
            removed = self.mod.delete_rule(g, key) if key else None
            await self._reply(event, f"已删除群规：{removed.get('name')}" if removed else "没找到这条群规（可用序号或 id）")
            return

        if sub == "清空":
            self.mod.set_rules(g, [])
            await self._reply(event, "已清空本群群规。")
            return

        await self._reply(event, (
            "群规指令怎么用：" + chr(10) +
            "· /群管 群规            看现有群规" + chr(10) +
            "· /群管 群规 添加 刷屏 | 连续刷无意义内容 | 10分钟" + chr(10) +
            "· /群管 群规 删除 2     删第 2 条" + chr(10) +
            "· /群管 群规 清空       全删"
        ))

    # ---------------- /审核（管理员） ----------------
    async def _cmd_mod(self, event: AstrMessageEvent, parts: list[str]) -> None:
        g = event.get_group_id()
        if not g:
            return
        sub = parts[1] if len(parts) > 1 else ""
        st = self._mod_settings(g)
        if sub == "试跑":
            val = (parts[2] if len(parts) > 2 else "").strip()
            if val in ("开", "on", "true", "1"):
                st = self.mod.set_settings(g, {"dry_run": True})
                await self._reply(event, "已开启试运行：只判定不执行。")
            elif val in ("关", "off", "false", "0"):
                self.mod.set_settings(g, {"dry_run": False})
                await self._reply(event, "已关闭试运行：判定后将真实撤回/禁言，请确认群规已调好。")
            else:
                await self._reply(event, "用法：/群管 审核 试跑 开|关")
            return
        if sub == "巡检":
            val = (parts[2] if len(parts) > 2 else "").strip()
            if val in ("开", "on", "true", "1"):
                self.mod.set_settings(g, {"auto_scan": True})
                await self._reply(event, "已开启本群关键词巡检（仅本群生效，零成本；AI 巡检请在插件页面开启）。")
            elif val in ("关", "off", "false", "0"):
                self.mod.set_settings(g, {"auto_scan": False})
                await self._reply(event, "已关闭本群巡检，恢复为「仅举报才审核」。")
            else:
                await self._reply(event, "用法：/群管 审核 巡检 开|关")
            return
        if sub == "自我介绍":
            await self._reply(event, "正在发送本群群管功能介绍…")
            if await self._send_group_intro(g, event.get_platform_id()):
                self._mark_intro_done(g, "manual")
                await self._reply(event, "已发送 ✅ 此后本群不再触发「首条消息欢迎」。")
            else:
                await self._reply(
                    event,
                    "发送失败。主动消息要求群内 24 小时内有人 @ 过机器人（11251），"
                    "先在本群 @ 我一下再试；入群事件那条路不受此限制。",
                )
            return
        if sub == "测试":
            text = " ".join(parts[2:]).strip()
            # 可选：在末尾加「前科=3」模拟该成员已有 3 次前科（不写任何数据）
            prior_override = None
            m_prior = re.search(r"(?:前科|prior)=(\d{1,3})", text)
            if m_prior:
                prior_override = int(m_prior.group(1))
                text = (text[:m_prior.start()] + text[m_prior.end():]).strip()
            if not text:
                await self._reply(event, "用法：/群管 审核 测试 <消息内容> [前科=3]")
                return
            rep = {"text": text, "sender": "test", "sender_name": "测试用户", "images": [], "ts": time.time(), "id": ""}
            v = self._mod_review_fast(g, rep, respect_switches=False, scene="审核测试") or await self._mod_review_llm(
                event, g, rep, [], "（测试）", respect_switches=False, scene="审核测试")
            if not v:
                await self._reply(event, "测试失败：模型未返回结果。")
                return
            d = self._mod_predict_action(g, v, rep, prior_override=prior_override)
            try:
                vh = self._mod_variant_scan(g, rep, {**st, "variant_check": True})
            except Exception:
                vh = None
            vh_text = (f"变体层：疑似命中（{vh['hint']}｜强度 {vh['strength']}｜{vh['count']} 处）"
                       if vh else "变体层：未命中")
            await self._reply(event, (
                f"【测试判定】违规={v.get('violation')} 依据={self.mod.get_dim_names(g).get(str(v.get('basis')),'')} "
                f"类型={v.get('category')} 严重度={v.get('severity')} 置信度={v.get('confidence')}" + chr(10) + ""
                f"理由：{v.get('reason')}" + chr(10) +
                f"拟执行：{d['action']} {d['duration']}"
                + (f"（前科 {d['prior_offenses']} 次，倍率 ×{d['escalation']}）" if d["escalation"] and d["escalation"] != 1.0 else "")
                + ("（智能分级已开启）" if d["smart"] else "（智能分级未开启）")
                + "（试跑不执行任何动作）" + chr(10) +
                vh_text
            ))
            return
        # 默认：状态
        s = self.mod.stats(g)
        await self._reply(event, (
            "【本群审核状态】" + chr(10) + ""
            f"举报审核：{'开启' if st.get('enabled') else '关闭'}｜试运行：{'开' if st.get('dry_run') else '关'}"
            f"｜巡检：{'开' if st.get('auto_scan') else '关'}" + chr(10) + ""
            f"群规：{len(self.mod.get_rules(g))} 条｜阈值：{st.get('confidence_threshold')}｜上限：{fmt_duration(int(st.get('max_mute_seconds') or 0))}" + chr(10) + ""
            f"累计：{s['total']} 次（违规 {s['hit']}）｜今日：{s['today_total']} 次｜缓存命中率：{s['cache_rate']:.0%}" + chr(10) + ""
            f"变体检测：{'开' if st.get('variant_check') else '关'}"
            f"（命中即撤回：{'开' if st.get('variant_recall') else '关'}）"
            f"｜待办：{s.get('todo_open', 0)} 条｜复犯累进：{'开' if st.get('escalate_enabled') else '关'}" + chr(10) + ""
            f"用法：/群管 审核 试跑 开|关 ｜ /群管 审核 巡检 开|关 ｜ /群管 审核 测试 <内容> ｜ /群管 审核 自我介绍"
        ))

    # ---------------- 插件 Page 接口 ----------------
    # ---------------- 机器人 / 群名（openid 太抽象，这里做人类可读映射） ----------------
    def _bot_label(self, platform_id: str) -> dict:
        """机器人展示信息：platform_id + appid 掩码 + 昵称（有缓存时）。"""
        info = {"platform_id": platform_id, "appid": "", "appid_masked": "", "name": "", "enable": True}
        for pl in self._platform_list():
            if pl.get("id") == platform_id:
                info["appid"] = pl.get("appid") or ""
                info["appid_masked"] = pl.get("appid_masked") or ""
                info["enable"] = bool(pl.get("enable", True))
                break
        info["name"] = str(self.mod.get_bot_meta(platform_id).get("name") or "")
        info["display"] = info["name"] or platform_id or "未知机器人"
        return info

    def _group_bot(self, group: str) -> str:
        """群归属哪个机器人：事件里记的 platform_id > 本地备注 > 唯一启用平台兜底。"""
        pid = self._known_groups.get(group) or str(self.mod.get_group_meta(group).get("platform_id") or "")
        if pid:
            return pid
        enabled = [p for p in self._platform_list() if p.get("enable")]
        return enabled[0]["id"] if len(enabled) == 1 else ""

    def _mod_group_entry(self, group: str) -> dict:
        meta = self.mod.get_group_meta(group)
        name = str(meta.get("name") or "")
        alias = str(meta.get("alias") or "")
        return {
            "group": group,
            "platform_id": self._group_bot(group),
            "name": name,
            "alias": alias,
            "display": alias or name,
            "name_state": str(meta.get("name_state") or ""),   # "" 未拉取 / ok / denied(无权限) / failed
            "name_error": str(meta.get("name_error") or ""),
        }

    async def _refresh_bot_name(self, platform_id: str, ttl: float = 120.0) -> str:
        """拉机器人自己的昵称（GET /users/@me，普通接口），失败返回空串。

        - 异常一律吞掉：昵称只是显示用，不能因为它把「策略列表」整页搞崩。
        - 带 TTL 节流：拉不到时别把每个列表请求都拖成两次 API 往返。
        """
        tried = getattr(self, "_bot_name_tried", None)
        if tried is None:
            tried = self._bot_name_tried = {}
        now = time.monotonic()
        if now - tried.get(platform_id, 0.0) < ttl:
            return str((self.mod.get_bot_meta(platform_id) or {}).get("name") or "")
        tried[platform_id] = now
        try:
            api = self._get_api(platform_id)
            info = await api.get_bot_info()
            name = str((info or {}).get("username") or "").strip()
            if name:
                self.mod.set_bot_meta(platform_id, name=name[:40])
            return name
        except Exception as e:  # noqa: BLE001 昵称拿不到不影响主流程
            self.logger.debug(f"[入群审批] 拉取机器人昵称失败 {platform_id}: {e}")
            return ""

    async def web_mod_groups(self):
        groups = self._all_known_group_list()
        overview = {g["group"]: g for g in self.mod.groups_overview()}
        items = []
        for g in groups:
            ov = overview.get(g)
            item = dict(ov) if ov else {
                "group": g, "enabled": True, "dry_run": True, "auto_scan": False,
                "rule_count": 0, "rules": [], "settings": self.mod.get_settings(g), "stats": self.mod.stats(g),
            }
            item.update(self._mod_group_entry(g))
            items.append(item)
        # 按机器人分桶：不同机器人管不同的群，各自独立、互不干扰
        buckets: dict[str, list] = {}
        for it in items:
            buckets.setdefault(it.get("platform_id") or "", []).append(it)
        bots = []
        for pid in sorted(buckets.keys(), key=lambda x: (x == "", x)):
            b = self._bot_label(pid)
            b["groups"] = buckets[pid]
            b["group_count"] = len(buckets[pid])
            bots.append(b)
        return json_response({"items": items, "bots": bots, "total": len(items)})

    async def web_mod_group_rename(self):
        """POST {group, alias} 手动备注群名（仅面板显示，不改变 openid）。"""
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        if not g:
            return json_response({"error": "missing group"})
        self.mod.set_group_alias(g, str(payload.get("alias") or ""))
        return json_response({"ok": True, **self._mod_group_entry(g)})

    async def web_mod_group_names_refresh(self):
        """POST {group?|platform_id?|all?, force?} 拉群名 + 机器人昵称。

        默认是「按需补齐」：已经有群名的群直接跳过，不重复打接口；
        需要强制重拉时带 force=true。
        群名接口（GET /v2/groups/{openid}/info）仅白名单机器人可用，
        无权限时返回 11253，这里记为 denied 并如实上报，页面可改用手动备注。
        """
        payload = await request.json(default={})
        one = str(payload.get("group") or "")
        if one:
            targets = [one]
        else:
            targets = self._all_known_group_list()
            pid_filter = str(payload.get("platform_id") or "")
            if pid_filter:
                targets = [x for x in targets if self._group_bot(x) == pid_filter]
        targets = targets[:60]
        # 已经拿到名字的群不再打扰接口（群名接口 30 QPM，且群名不会频繁变），
        # 需要重拉时显式带 force=true（页面「重新拉取本群群名」或群内 /群名）。
        force = bool(payload.get("force"))
        skipped = 0
        if not force:
            keep = []
            for g in targets:
                m = self.mod.get_group_meta(g)
                if m.get("name") or m.get("name_state") == "denied":
                    skipped += 1
                    continue
                keep.append(g)
            targets = keep
        updated, denied, no_bot, failed, seen_bots = 0, 0, 0, [], set()
        first_error = ""
        for grp in targets:
            pid = self._group_bot(grp)
            if not pid:
                no_bot += 1
                continue
            if pid not in seen_bots:
                seen_bots.add(pid)
                try:
                    await self._refresh_bot_name(pid)
                except Exception as e:      # noqa: BLE001
                    self.logger.warning(f"[审核] 拉机器人昵称失败: {e}")
            try:
                api = self._get_api(pid)
            except QQApiError as e:
                failed.append({"group": grp, "error": str(e)})
                continue
            data, err = await api.fetch_group_info(grp)
            if data and data.get("group_name"):
                self.mod.set_group_meta(grp, name=str(data.get("group_name"))[:60],
                                        platform_id=pid, name_state="ok", name_error="")
                updated += 1
            else:
                code = getattr(err, "code", None)
                is_denied = code == 11253
                msg = str(err) if err else "未知错误"
                self.mod.set_group_meta(grp, platform_id=pid,
                                        name_state=("denied" if is_denied else "failed"),
                                        name_error=msg[:200])
                if is_denied:
                    denied += 1
                else:
                    failed.append({"group": grp, "error": msg})
                first_error = first_error or msg
            await asyncio.sleep(0.08)   # 群信息接口 30 QPM，留点余量
        bots = [self._bot_label(pid) for pid in sorted(seen_bots)]
        return json_response({
            "ok": True, "checked": len(targets), "updated": updated,
            "denied": denied, "no_bot": no_bot, "failed": failed[:10],
            "skipped": skipped, "force": force,
            "message": _refresh_hint(updated, denied, no_bot, failed, first_error, skipped),
            "bots": bots,
        })

    def _all_known_group_list(self) -> list[str]:
        groups = set(self._known_groups.keys())
        groups |= set((self._nick_cache or {}).keys())
        groups |= set((self._recent_group_msgs or {}).keys())
        groups |= {x["group"] for x in self.mod.groups_overview()}
        return sorted(x for x in groups if x)

    async def web_mod_detail(self):
        g = request.query.get("group", "")
        if not g:
            return json_response({"error": "missing group"})
        return json_response({
            **self._mod_group_entry(g),
            "bot": self._bot_label(self._group_bot(g)),
            "rules": self.mod.get_rules(g),
            "settings": self.mod.get_settings(g),
            "templates": self.mod.get_templates(g),
            "templates_override": (self.mod.templates.get("groups") or {}).get(g) or {},
            "templates_global": self.mod.templates.get("global") or {},
            "default_templates": moderation.DEFAULT_TEMPLATES,
            "dim_names": self.mod.get_dim_names(g),
            "cat_names": self.mod.get_cat_names(g),
            "default_dim_names": moderation.DEFAULT_DIM_NAMES,
            "default_cat_names": moderation.DEFAULT_CAT_NAMES,
            "admins": self.mod.get_admins(g),
            "stats": self.mod.stats(g),
        })

    async def web_mod_rules_save(self):
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        rules = payload.get("rules")
        if not g or not isinstance(rules, list):
            return json_response({"error": "invalid payload"})
        clean = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                continue
            try:
                secs = int(r.get("seconds") or 3600)
            except (TypeError, ValueError):
                secs = 3600
            clean.append({
                "id": str(r.get("id") or f"rule_{i+1:02d}"),
                "name": str(r.get("name") or f"规则{i+1}")[:60],
                "desc": str(r.get("desc") or "")[:600],
                "category": str(r.get("category") or "other")[:30],
                "severity": str(r.get("severity") or "medium")[:10],
                "seconds": max(60, min(secs, 30 * 86400)),
                "enabled": bool(r.get("enabled", True)),
                "keywords": [str(k)[:200] for k in (r.get("keywords") or [])][:20],
            })
        self.mod.set_rules(g, clean)
        return json_response({"saved": True, "count": len(clean)})

    async def web_mod_rules_transfer(self):
        """POST /mod/rules/transfer — 把一个群的群规复制到另一个群。

        append 追加（默认，id 自动去重）/ replace 覆盖目标群现有群规。
        源群取的是**已保存**的群规，页面上没保存的改动不算，前端要提示。
        """
        payload = await request.json(default={})
        src = str(payload.get("from") or "").strip()
        dst = str(payload.get("to") or "").strip()
        mode = str(payload.get("mode") or "append").lower()
        ids = payload.get("ids")
        if ids is not None and not isinstance(ids, list):
            ids = None
        if not src or not dst:
            return json_response({"ok": False, "error": "缺少源群或目标群"})
        if src == dst:
            return json_response({"ok": False, "error": "源群和目标群相同"})
        res = self.mod.transfer_rules(src, dst, mode, ids=ids)
        if res.get("ok"):
            scope = f"勾选 {len(ids)} 条" if ids else "全部"
            self.logger.info(
                f"[群规] 转移：{src} → {dst}，{mode} 模式，{scope}，新增 {res['count']} 条，"
                f"目标群现有 {res['total']} 条"
            )
        return json_response(res)

    async def web_mod_settings_save(self):
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        if not g:
            return json_response({"error": "missing group"})
        allowed = set(moderation.ModerationStore.DEFAULT_SETTINGS.keys())
        patches = {k: v for k, v in (payload.get("settings") or {}).items() if k in allowed}
        if patches.get("keywords") is not None and not isinstance(patches.get("keywords"), dict):
            patches.pop("keywords", None)
        if "confidence_threshold" in patches:
            try:
                patches["confidence_threshold"] = max(0.0, min(float(patches["confidence_threshold"]), 1.0))
            except (TypeError, ValueError):
                patches.pop("confidence_threshold", None)
        for k in ("context_size", "max_mute_seconds", "report_limit_per_hour", "recall_max_age_seconds",
                  "escalate_window_days", "variant_strike_window_days", "variant_strike_threshold",
                  "variant_strike_mute_threshold", "variant_strike_mute_seconds",
                  "variant_strike_cooldown_hours"):
            if k in patches:
                try:
                    patches[k] = max(0, int(patches[k]))
                except (TypeError, ValueError):
                    patches.pop(k, None)
        # 变体累计升级：把越界值夹到合理区间（阈值 1~50、窗口 1~90 天、冷却 0~168 小时）
        for k, lo, hi in (("variant_strike_window_days", 1, 90),
                          ("variant_strike_threshold", 1, 50),
                          ("variant_strike_mute_threshold", 1, 50),
                          ("variant_strike_mute_seconds", 60, 86400),
                          ("variant_strike_cooldown_hours", 0, 168)):
            if k in patches:
                try:
                    patches[k] = max(lo, min(int(patches[k]), hi))
                except (TypeError, ValueError):
                    patches.pop(k, None)
        if "variant_min_chars" in patches:
            try:
                patches["variant_min_chars"] = max(2, min(int(patches["variant_min_chars"]), 12))
            except (TypeError, ValueError):
                patches.pop("variant_min_chars", None)
        for k in ("escalate_step", "escalate_max_multiplier"):
            if k in patches:
                try:
                    patches[k] = max(0.0, min(float(patches[k]), 20.0))
                except (TypeError, ValueError):
                    patches.pop(k, None)
        if isinstance(patches.get("whitelist"), list):
            patches["whitelist"] = [str(x)[:64] for x in patches["whitelist"]][:200]
        return json_response({"saved": True, "settings": self.mod.set_settings(g, patches)})

    async def web_mod_templates_save(self):
        payload = await request.json(default={})
        scope = str(payload.get("scope") or "global")
        values = payload.get("templates") or {}
        if not isinstance(values, dict):
            return json_response({"error": "invalid templates"})
        clean = {}
        for k, v in values.items():
            if k not in moderation.DEFAULT_TEMPLATES and k not in ("dim_names", "cat_names", "mod_report_limit"):
                continue
            if k in ("dim_names", "cat_names"):
                if isinstance(v, dict):
                    clean[k] = {str(a)[:30]: str(b)[:60] for a, b in v.items()}
                continue
            clean[k] = str(v)[:400]
        if scope == "global":
            # 维度/分类显示名是全局的，单独存放
            dims = clean.pop("dim_names", None)
            cats = clean.pop("cat_names", None)
            if dims or cats:
                self.mod.templates["dim_names"] = {**(self.mod.templates.get("dim_names") or {}), **(dims or {})}
                self.mod.templates["cat_names"] = {**(self.mod.templates.get("cat_names") or {}), **(cats or {})}
                self.mod._save(self.mod._tpl_path, self.mod.templates)
            self.mod.set_templates("global", clean)
        else:
            clean.pop("dim_names", None)
            clean.pop("cat_names", None)
            self.mod.set_templates(scope, clean)
        return json_response({"saved": True})

    async def web_mod_verdicts_revoke(self):
        """POST /mod/verdicts/revoke {id, reason} — 面板撤销（操作者为当前登录管理员）。"""
        payload = await request.json(default={})
        try:
            vid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return json_response({"ok": False, "error": "invalid id"})
        reason = str(payload.get("reason") or "")[:200]
        rec = self.mod.get_verdict(vid)
        if rec is None:
            return json_response({"ok": False, "error": "not_found"})
        group = str(payload.get("group") or rec.get("group") or "")
        # 面板登录者就是 AstrBot 管理员，按 bot admin 处理（不受「不能撤自己」限制）
        res = await self._revoke_verdict(
            group, vid, operator="panel", operator_name="面板管理员",
            reason=reason, is_bot_admin=True,
        )
        res.pop("record", None)
        return json_response(res)

    async def web_mod_verdicts_prune(self):
        """POST /mod/verdicts/prune {days, group} — 删除 N 天以前的记录（仅手动触发）。"""
        payload = await request.json(default={})
        try:
            days = int(payload.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        group = str(payload.get("group") or "")
        if days <= 0:
            return json_response({"ok": False, "error": "days 必须大于 0"})
        removed = self.mod.prune_older_than(days, group)
        self.logger.info(f"[审核] 手动清理判定记录：{days} 天以前，删除 {removed} 条（group={group or '全部'}）")
        return json_response({"ok": True, "removed": removed, "days": days})

    async def web_mod_verdicts_export(self):
        """GET /mod/verdicts/export?group=&days=&limit= — 导出判定记录。

        服务端只做筛选与截断，CSV 交给面板前端拼（不依赖 AstrBot 版本的响应体能力）。
        """
        g = str(request.query.get("group", "") or "")
        days = request.query.get("days", 0, type=int) or 0
        limit = min(5000, max(1, request.query.get("limit", 1000, type=int)))
        since = int(time.time()) - days * 86400 if days > 0 else 0
        items: list[dict] = []
        for v in self.mod._iter_verdicts():          # 新 → 旧
            if (not g or v.get("group") == g) and (not since or int(v.get("created_at") or 0) >= since):
                items.append(v)
                if len(items) >= limit:
                    break
        items.reverse()                              # 与旧行为一致：按时间从旧到新
        return json_response({"items": items, "total": len(items)})

    async def web_mod_verdicts(self):
        g = request.query.get("group", "")
        limit = request.query.get("limit", 100, type=int)
        return json_response({"items": self.mod.list_verdicts(g, limit)})

    async def web_mod_feedback(self):
        payload = await request.json(default={})
        try:
            vid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return json_response({"error": "invalid id"})
        label = str(payload.get("label") or "")[:20]
        note = str(payload.get("note") or "")[:300]
        if not self.mod.update_verdict(vid, feedback=label, feedback_note=note):
            return json_response({"error": "not found"})
        # 人工标注回灌到提示词示例（只做提示，不自动改判定规则）。
        # 以前是往内存里 += 一行，重启就丢；现在从判定记录重建，持久化生效。
        self._rebuild_mod_few_shot()
        return json_response({"saved": True, "label": label,
                              "samples": len(self.mod.feedback_samples(limit=20).get(label) or [])})

    # ------------------------------------------------------------------
    # 成员档案 Web API
    # ------------------------------------------------------------------
    async def web_members(self):
        """GET /members?group=xxx&status=&keyword=&sort=&page=&size= 成员档案列表。"""
        g = str(request.query.get("group", "") or "")
        if not g:
            return json_response({"error": "missing group"})
        status = str(request.query.get("status", "all") or "all")
        keyword = str(request.query.get("keyword", "") or "")
        sort = str(request.query.get("sort", "last_seen") or "last_seen")
        page = max(1, request.query.get("page", 1, type=int))
        size = min(200, max(5, request.query.get("size", 30, type=int)))
        rows, total = self.members.list_members(
            g, status=status, keyword=keyword, sort=sort,
            offset=(page - 1) * size, limit=size,
        )
        items = []
        for r in rows:
            item = dict(r)
            item["short"] = member_registry_lib.short_id(r.get("openid"))
            item["last_seen_text"] = fmt_ts(r.get("last_seen") or 0)
            item["first_seen_text"] = fmt_ts(r.get("first_seen") or 0)
            item["left_at_text"] = fmt_ts(r.get("left_at") or 0) if r.get("left_at") else ""
            items.append(item)
        return json_response({
            "items": items, "total": total, "page": page, "size": size,
            "stats": self.members.stats(g),
        })

    async def web_members_stats(self):
        """GET /members/stats?group=xxx 档案统计。"""
        g = str(request.query.get("group", "") or "")
        if not g:
            return json_response({"error": "missing group"})
        return json_response(self.members.stats(g))

    async def web_members_update(self):
        """POST /members/update body: {group, openid, nickname?, note?}"""
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        openid = str(payload.get("openid") or "")
        if not g or not openid:
            return json_response({"error": "invalid payload"})
        if not self.members.has(g, openid):
            return json_response({"error": "member not found"})
        if "nickname" in payload:
            self.members.set_name(g, openid, str(payload.get("nickname") or ""), source="manual")
        if "note" in payload:
            self.members.set_note(g, openid, str(payload.get("note") or ""))
        rec = self.members.get(g, openid) or {}
        rec = dict(rec)
        rec["short"] = member_registry_lib.short_id(openid)
        return json_response({"ok": True, "item": rec})

    async def web_members_delete(self):
        """POST /members/delete body: {group, openid}"""
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        openid = str(payload.get("openid") or "")
        if not g or not openid:
            return json_response({"error": "invalid payload"})
        ok = self.members.delete(g, openid)
        return json_response({"ok": ok, "stats": self.members.stats(g)})

    async def web_members_clear_left(self):
        """POST /members/clear_left body: {group}"""
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        if not g:
            return json_response({"error": "invalid payload"})
        n = self.members.delete_left(g)
        return json_response({"removed": n, "stats": self.members.stats(g)})

    async def web_members_import(self):
        """POST /members/import body: {group, text}

        每行一条：`openid,昵称,备注`（逗号/制表符/全角逗号都行），
        最少只需 openid。给"从别处导出一批名字"用。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        text = str(payload.get("text") or "")
        if not g or not text.strip():
            return json_response({"error": "invalid payload"})
        rows = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = [p.strip() for p in re.split(r"[,，\t]+", line)]
            oid = parts[0] if parts else ""
            if not oid:
                continue
            row = {"openid": oid}
            if len(parts) > 1 and parts[1]:
                row["nickname"] = parts[1]
            if len(parts) > 2 and parts[2]:
                row["note"] = parts[2]
            rows.append(row)
        added, updated = self.members.upsert_many(g, rows)
        return json_response({
            "added": added, "updated": updated,
            "parsed": len(rows), "stats": self.members.stats(g),
        })

    async def web_members_sync(self):
        """POST /members/sync body: {group} 用官方群成员列表接口补昵称。

        该接口目前是内邀白名单能力（11253），失败时如实返回错误码，
        前端按 code 给提示，不做"看起来成功了"的假象。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        if not g:
            return json_response({"error": "invalid payload"})
        api = self._api_for_group_or_default(g)
        if api is None:
            return json_response({"error": "no_api", "message": "没有可用的机器人实例"})
        try:
            members = await api.list_group_members(g)
        except QQApiError as e:
            return json_response({
                "error": "api_error", "code": e.code,
                "message": "官方群成员列表接口是内邀能力（11253），本机器人暂无权限"
                if e.code == 11253 else str(e),
            })
        except Exception as e:  # noqa: BLE001
            return json_response({"error": "api_error", "message": str(e)})
        rows = [{
            "openid": m.get("member_openid") or "",
            "nickname": m.get("username") or "",
            "role": m.get("member_role") or "",
            "user_openid": m.get("union_openid") or m.get("user_openid") or "",
            "joined_at": m.get("joined_at") or "",
        } for m in members if m.get("member_openid")]
        added, updated = self.members.upsert_many(g, rows)
        return json_response({
            "ok": True, "added": added, "updated": updated,
            "count": len(rows), "stats": self.members.stats(g),
        })

    async def web_mod_admins(self):
        """GET /mod/admins?group=xxx 变体提醒名单（群内管理员用 /管理登记 自助登记）。"""
        g = request.query.get("group", "")
        if not g:
            return json_response({"error": "missing group"})
        items = self.mod.get_admins(g)
        return json_response({"items": items, "count": len(items),
                              "notify": bool(self.mod.get_settings(g).get("variant_strike_notify", True))})

    async def web_mod_admins_remove(self):
        """POST /mod/admins/remove 把某人移出提醒名单。

        body: {group, openid}
        成员自己发「/管理登记 取消」也有同样效果，这里是为了不用麻烦他本人。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        openid = str(payload.get("openid") or "")
        if not g or not openid:
            return json_response({"error": "invalid payload"})
        removed = self.mod.unregister_admin(g, openid)
        items = self.mod.get_admins(g)
        return json_response({"removed": removed, "items": items, "count": len(items)})

    async def web_mod_todo(self):
        """GET /mod/todo?group=xxx 待办队列（低置信未处理 + 变体疑似，都没人工处理过的）。"""
        g = request.query.get("group", "")
        if not g:
            return json_response({"error": "missing group"})
        limit = request.query.get("limit", 100, type=int)
        items = self.mod.todo_items(g, limit)
        for it in items:
            it["created_at_text"] = fmt_ts(it.get("created_at") or 0, "%m-%d %H:%M")
            it["suggest_text"] = fmt_duration(int(it.get("suggest_seconds") or 0))
        return json_response({"items": items, "count": self.mod.todo_count(g),
                              "window_days": self.mod.TODO_WINDOW_DAYS})

    async def web_mod_todo_handle(self):
        """POST /mod/todo/handle 处理待办。

        body: {group, ids: [判定记录 id], action: ignore|recall|mute, seconds?, operator?}
        - ignore：只标记「已看过/不处理」，不动消息、不禁言；
        - recall：按群配置撤回该消息（人工确认版撤回）；
        - mute：人工确认后禁言（秒数取 body.seconds，缺省用严重度建议值）。
        无论哪种都会把对应记录标记为已处理，从待办里移出。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        raw_ids = payload.get("ids") or ([payload.get("id")] if payload.get("id") else [])
        action = str(payload.get("action") or "ignore").lower()
        operator = str(payload.get("operator") or "面板")[:64]
        if not g or not raw_ids:
            return json_response({"error": "missing group or ids"})
        try:
            want = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            return json_response({"error": "invalid ids"})
        if action not in ("ignore", "recall", "mute"):
            return json_response({"error": "invalid action"})

        items = {int(i.get("id") or 0): i for i in self.mod.todo_items(g, limit=1000)}
        picked = [items[i] for i in want if i in items]
        if not picked:
            return json_response({"error": "待办记录不存在或已处理"})

        done: list[dict] = []
        muted = recalled = 0
        if action in ("recall", "mute"):
            api = self._api_for_group_or_default(g)
            if api is None:
                return json_response({"error": "无可用 API 客户端"})

        for it in picked:
            vid = int(it.get("id") or 0)
            target = str(it.get("target") or "")
            note = ""
            if action == "recall":
                # 找到被举报消息 ID（判定记录里存的是 msg_id）
                mid = ""
                rec = self.mod.get_verdict(vid)
                mid = str((rec or {}).get("msg_id") or "")
                if mid:
                    try:
                        await api.recall_group_message(g, mid)
                        self._add_recall_record(group_openid=g, platform_id=self._group_bot(g),
                                                message_id=mid, content=str(it.get("text") or ""),
                                                source="todo_manual", operator=operator,
                                                operator_name="待办处理")
                        recalled += 1
                        note = "已撤回"
                    except Exception as e:                       # noqa: BLE001
                        note = _recall_hint(e)
                else:
                    note = "记录里没有消息 ID（引用定位失败），未撤回"
            elif action == "mute":
                if not target:
                    note = "无法确定被举报消息的发送者，未禁言"
                else:
                    try:
                        secs = int(payload.get("seconds") or it.get("suggest_seconds") or 600)
                    except (TypeError, ValueError):
                        secs = 600
                    secs = max(60, min(secs, int(self._mod_settings(g).get("max_mute_seconds") or 86400)))
                    expire_ts = time.time() + secs
                    try:
                        await api.set_mute(g, target, mute_expire_at=QQApi.format_rfc3339(expire_ts), op="add")
                        self._add_mute_record(
                            group_openid=g, platform_id=self._group_bot(g), member_openid=target,
                            username=str(it.get("target_name") or ""),
                            reason=f"待办人工处理：{str(it.get('reason') or '')[:100]}",
                            seconds=secs, expire_ts=expire_ts,
                            operator=operator, operator_name="待办处理",
                        )
                        muted += 1
                        note = f"已禁言 {fmt_duration(secs)}"
                    except Exception as e:                       # noqa: BLE001
                        note = f"禁言失败：{e}"
            else:
                note = "已标记为不处理"
            done.append({"id": vid, "ok": not note.startswith(("撤回失败", "禁言失败", "无可用")), "note": note})

        self.mod.mark_todo(want, "done" if action != "ignore" else "ignored",
                           operator=operator, note=f"{action}: {','.join(d['note'] for d in done)[:120]}")
        self.logger.info(f"[审核] 待办处理：group={g} action={action} 条数={len(done)} "
                         f"禁言={muted} 撤回={recalled}")
        return json_response({"handled": len(done), "muted": muted, "recalled": recalled,
                              "items": done, "count": self.mod.todo_count(g)})

    async def web_mod_stats(self):
        g = request.query.get("group", "")
        return json_response({"stats": self.mod.stats(g), "groups": len(self._all_known_group_list())})

    async def web_mod_test(self):
        payload = await request.json(default={})
        g = str(payload.get("group") or "")
        text = str(payload.get("text") or "")
        try:
            prior_override = int(payload.get("prior")) if payload.get("prior") not in (None, "") else None
        except (TypeError, ValueError):
            prior_override = None
        if not g or not text:
            return json_response({"error": "missing group or text"})
        rep = {"text": text, "sender": "test", "sender_name": "测试用户", "images": [], "ts": time.time(), "id": ""}
        v = self._mod_review_fast(g, rep, respect_switches=False, scene="页面试跑")
        used_llm = False
        pinfo: dict = {"id": "", "display": "", "source": "none"}
        if not v:
            # 模型解析：与群内实际审核同一条链路（配置 > 该群当前对话模型 > 全局默认），
            # 不再像以前那样在面板里随便挑第一个 provider。
            pinfo = await self._mod_provider_info(group=g)
            if not pinfo.get("id"):
                return json_response({"error": "没有可用的对话模型：请先在 AstrBot 里配置 Provider，或在「审核模型」里手动指定"})
            v = await self._mod_review_llm(None, g, rep, [], "（页面试跑）",
                                           pid_override=str(pinfo.get("id") or ""),
                                           respect_switches=False, scene="页面试跑")
            used_llm = True
        if not v:
            return json_response({"error": "判定失败：模型未返回结果"})
        d = self._mod_predict_action(g, v, rep, prior_override=prior_override)
        # 变体层也一起试跑：不看本群开关（页面测试就是要看效果），只借本群的阈值配置
        try:
            vopts = {**self._mod_settings(g), "variant_check": True}
            variant = self._mod_variant_scan(g, rep, vopts)
        except Exception:
            variant = None
        return json_response({
            "verdict": v,
            "action": d,
            "duration": d.get("duration") or "",
            "used_llm": used_llm,
            "provider": pinfo,
            "variant": variant,
        })

    # ------------------------------------------------------------------
    # 词库（公开词库 + 上传 + 白名单 + 命中测试）
    # ------------------------------------------------------------------
    @staticmethod
    def _lex_payload(item: dict) -> dict:
        """给前端的词库条目（去掉绝对路径等内部字段）。"""
        return {
            "id": item["id"], "name": item["name"], "source": item["source"],
            "kind": item.get("kind", "rules"), "match": item.get("match", "auto"),
            "enabled": bool(item.get("enabled")), "count": int(item.get("count") or 0),
            "literal_count": int(item.get("literal_count") or 0),
            "regex_count": int(item.get("regex_count") or 0),
            "cat_counts": item.get("cat_counts") or {},
            "description": item.get("description") or "",
            "version": item.get("version") or "", "author": item.get("author") or "",
            "sources": item.get("sources") or [],
            "default_enabled": item.get("default_enabled"),
            "errors": (item.get("errors") or [])[:5],
            "warnings": (item.get("warnings") or [])[:5],
            "error_count": int(item.get("error_count") or 0),
            "warning_count": int(item.get("warning_count") or 0),
            "mtime": item.get("mtime"), "size": int(item.get("size") or 0),
            # 只带「用途声明」文本，不带任何词条
            "notice": str(item.get("notice") or ""),
        }

    # ------------------------------------------------------------------
    # 词库：一键获取上游词库
    # ------------------------------------------------------------------
    def _lexicon_upstream_conf(self) -> dict:
        """读「词库」分组的三个设置，缺省时回落到常量默认。"""
        url = str(self._read_config_value(("lexicon", "upstream_url"), default=LEXICON_UPSTREAM_URL) or "").strip()
        cat = str(self._read_config_value(("lexicon", "upstream_category"), default="other") or "other").strip()
        auto = bool(self._read_config_value(("lexicon", "upstream_auto_enable"), default=False))
        mirror = str(self._read_config_value(("lexicon", "upstream_mirror"), default="") or "").strip()
        force = bool(self._read_config_value(("lexicon", "upstream_force_mirror"), default=False))
        if cat not in moderation.DEFAULT_CAT_NAMES:
            cat = "other"
        return {"url": url, "category": cat, "auto_enable": auto,
                "mirror": mirror, "force_mirror": force, "name": LEXICON_UPSTREAM_NAME}

    @staticmethod
    def _check_url_public(url: str) -> str:
        """只放行指向公网 http/https 的地址，挡掉 SSRF（本机 / 内网 / 元数据接口）。

        返回错误文案，空串表示通过。地址由管理员填写，但面板一旦被越权访问，
        这个入口就能拿来探测内网，所以在真正发请求前再校验一次。
        """
        try:
            sp = urllib.parse.urlsplit((url or "").strip())
        except Exception:                                          # noqa: BLE001
            return "地址格式不对"
        if sp.scheme.lower() not in ("http", "https"):
            return "只支持 http/https 地址"
        host = (sp.hostname or "").strip().rstrip(".").lower()
        if not host:
            return "地址里没有主机名"
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
            return "不抓取本机 / 内网地址"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if QQGroupManager._ip_is_public(ip):
                return ""
            return "不抓取本机 / 内网地址"
        # 域名：解析结果全部为公网才放行（解析失败就交给 urlopen 报错）
        try:
            for info in socket.getaddrinfo(host, None):
                addr = info[4][0]
                if not QQGroupManager._ip_is_public(ipaddress.ip_address(addr)):
                    return "该域名解析到本机 / 内网地址，已拒绝"
        except (socket.gaierror, ValueError):
            pass
        except Exception:                                          # noqa: BLE001
            pass
        return ""

    @staticmethod
    def _ip_is_public(ip) -> bool:
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified)

    @staticmethod
    def _fetch_upstream_raw(url: str, timeout: float = LEXICON_UPSTREAM_TIMEOUT) -> bytes:
        """阻塞下载（放到线程里跑）。中文路径必须转义，否则 urllib 会按 ascii 编码报错。"""
        err = QQGroupManager._check_url_public(url)
        if err:
            raise ValueError(err)
        safe_url = urllib.parse.quote(url.strip(), safe=":/?&=#%+")
        req = urllib.request.Request(
            safe_url, headers={"User-Agent": "astrbot-plugin-qq-group-manager/lexicon-fetch"}
        )
        # 重定向也要复检，避免「公网地址 302 到 127.0.0.1」
        opener = urllib.request.build_opener(_PublicOnlyRedirectHandler)
        with opener.open(req, timeout=timeout) as resp:
            return resp.read(LEXICON_UPSTREAM_MAX_BYTES + 1)

    @staticmethod
    def _mirror_url(url: str, mirror: str) -> str:
        """加速地址两种写法：模板（含 {url}）或前缀（拼在原始地址前面）。"""
        mirror = (mirror or "").strip()
        if not mirror:
            return ""
        if "{url}" in mirror:
            return mirror.replace("{url}", url)
        if "{raw}" in mirror:
            return mirror.replace("{raw}", url)
        return mirror.rstrip("/") + "/" + url

    async def _download_once(self, url: str, timeout: float) -> tuple[str, str]:
        """下载一次并解码，返回 (文本, 错误)。"""
        try:
            raw = await asyncio.to_thread(self._fetch_upstream_raw, url, timeout)
        except Exception as e:                                   # noqa: BLE001
            return "", f"下载失败：{e}"
        if not raw:
            return "", "上游返回了空内容"
        if len(raw) > LEXICON_UPSTREAM_MAX_BYTES:
            return "", f"文件超过 {LEXICON_UPSTREAM_MAX_BYTES // 1024 // 1024} MB 上限"
        text = ""
        for enc in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "ignore")
        head = text.lstrip()[:200].lower()
        if head.startswith("<!doctype html") or head.startswith("<html"):
            # 常见错法：把 GitHub「网页」地址当原始文件地址（加速站返回错误页也长这样）
            return "", "这个地址返回的是网页不是词表，八成填成了仓库页面（要用 raw 原始文件地址）"
        return text, ""

    async def _download_upstream(self, url: str, mirror: str = "",
                                 force_mirror: bool = False) -> tuple[str, str, str, str]:
        """按「直连 → 加速」顺序尝试，返回 (文本, 错误, 来源, 实际地址)。

        配了加速地址时直连超时缩短到 8 秒，免得在连不上的网络里干等 15 秒。
        """
        mirror_url = self._mirror_url(url, mirror)
        if force_mirror and mirror_url:
            attempts: list[tuple[str, str, float]] = [("mirror", mirror_url, LEXICON_UPSTREAM_TIMEOUT)]
        elif mirror_url:
            attempts = [("direct", url, LEXICON_UPSTREAM_FAST_TIMEOUT),
                        ("mirror", mirror_url, LEXICON_UPSTREAM_TIMEOUT)]
        else:
            attempts = [("direct", url, LEXICON_UPSTREAM_TIMEOUT)]

        errors: list[str] = []
        for source, target, timeout in attempts:
            text, err = await self._download_once(target, timeout)
            if not err:
                return text, "", source, target
            errors.append(f"{'直连' if source == 'direct' else '加速'}：{err}")
        return "", "；".join(errors), "", ""

    async def _fetch_upstream_lexicon(self, url: str = "", name: str = "", category: str = "",
                                     auto_enable: Optional[bool] = None, mirror: str = "",
                                     force_mirror: Optional[bool] = None) -> dict:
        """把上游词表下载下来写成本地词库：同名则覆盖更新，避免每点一次多一个库。"""
        conf = self._lexicon_upstream_conf()
        url = (url or conf["url"]).strip()
        cat = (category or conf["category"]).strip()
        if cat not in moderation.DEFAULT_CAT_NAMES:
            cat = "other"
        auto = conf["auto_enable"] if auto_enable is None else bool(auto_enable)
        use_mirror = conf["mirror"] if mirror is None else str(mirror or "").strip()
        force = conf["force_mirror"] if force_mirror is None else bool(force_mirror)
        name = (name or LEXICON_UPSTREAM_NAME).strip()[:40] or LEXICON_UPSTREAM_NAME
        started = time.time()

        if not url:
            return {"ok": False, "url": "",
                    "error": "还没填词表地址：插件不内置任何下载链接，请自行找一份词表填进来，或直接上传文件"}
        if not re.match(r"^https?://", url, re.I):
            return {"ok": False, "error": "只支持 http/https 地址", "url": url}
        if "@" in urllib.parse.urlsplit(url).netloc:
            return {"ok": False, "error": "地址里不要带用户名密码", "url": url}
        if force and not use_mirror:
            return {"ok": False, "error": "勾了「只走加速」但没填加速地址", "url": url}

        text, err, source, used_url = await self._download_upstream(url, use_mirror, force)
        if err:
            self.logger.warning(f"[词库] 一键获取失败：{err}（{url}）")
            return {"ok": False, "error": err, "url": url, "mirror": use_mirror}

        exist = next(
            (x for x in self.lexicons.list_lexicons()
             if x.get("source") != "builtin" and x.get("name") == name),
            None,
        )
        prev_count = int(exist.get("count") or 0) if exist else 0
        via = "加速" if source == "mirror" else "直连"
        r = self.lexicons.upload(
            name=name, content=text, fmt="txt", default_category=cat,
            description=f"一键获取自动生成（{via}），来源：{url}"[:300],
            mode="overwrite" if exist else "new",
            target_id=exist["id"] if exist else "",
            match_mode=lexicon_lib.MATCH_LITERAL,
        )
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "写入词库失败", "url": url}
        # upload 新建时默认启用，这里按设置改回用户要的状态
        self.lexicons.set_enabled(r["id"], auto)
        count = int(r.get("count") or 0)
        self.logger.info(
            f"[词库] 一键获取「{r.get('name')}」{count} 条"
            f"（{'覆盖' if exist else '新建'}·{via}，{'已启用' if auto else '未启用'}）"
        )
        # 走加速站时词条数剧烈变化，多半是镜像给了错误页/别的版本，提醒一句
        note = ""
        if source == "mirror":
            note = "本次走的是第三方加速地址，建议核对一下条数是否合理"
        return {
            "ok": True, "url": url, "used_url": used_url, "source": via,
            "id": r["id"], "name": r.get("name"), "count": count,
            "prev_count": prev_count,
            "mode": "overwrite" if exist else "new", "enabled": auto,
            "errors": (r.get("errors") or [])[:5],
            "warnings": (r.get("warnings") or [])[:5],
            "note": note,
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    async def web_lexicons(self):
        if request.query.get("refresh"):
            self.lexicons.scan(force=True)
        return json_response({
            "stats": self.lexicons.stats(),
            "lexicons": [self._lex_payload(x) for x in self.lexicons.list_lexicons()],
            "allow": sorted(self.lexicons.allow_words()),
            "manual_allow": list(self.lexicons.state.get("allow") or []),
            "categories": [{"key": k, "name": v} for k, v in moderation.DEFAULT_CAT_NAMES.items()],
            "upstream": self._lexicon_upstream_conf(),
        })

    async def web_lexicon_detail(self):
        lid = str(request.query.get("id") or "")
        item = self.lexicons.get(lid)
        if not item:
            return json_response({"error": "词库不存在"})
        d = self._lex_payload(item)
        d["enabled"] = self.lexicons.is_enabled(item)
        # 词条内容不下发展示：只给统计（每类条数 / 长度分布），避免在界面上把敏感词摊开
        literals = item.get("literals") or {}
        regexes = item.get("regexes") or {}
        dist: dict[str, int] = {}
        for words in literals.values():
            for w in words:
                k = str(len(w))
                dist[k] = dist.get(k, 0) + 1
        d["word_display"] = "hidden"
        d["shown"] = False
        d["length_dist"] = dict(sorted(dist.items(), key=lambda kv: int(kv[0])))
        d["regex_count"] = sum(len(v) for v in regexes.values())
        d["regex_categories"] = sorted(regexes.keys())
        return json_response({"lexicon": d})

    async def web_lexicon_toggle(self):
        p = await request.json(default={})
        lid = str(p.get("id") or "")
        if not self.lexicons.get(lid):
            return json_response({"error": "词库不存在"})
        st = self.lexicons.set_enabled(lid, bool(p.get("enabled")))
        self.logger.info(f"[词库] {lid} -> {'启用' if p.get('enabled') else '停用'}")
        return json_response({"ok": True, "stats": st})

    async def web_lexicon_apply(self):
        p = await request.json(default={})
        ids = p.get("ids")
        if not isinstance(ids, list):
            return json_response({"error": "ids 必须是数组"})
        return json_response({"ok": True, "stats": self.lexicons.apply([str(x) for x in ids])})

    async def web_lexicon_upload(self):
        p = await request.json(default={})
        r = self.lexicons.upload(
            name=str(p.get("name") or ""),
            content=str(p.get("content") or ""),
            fmt=str(p.get("format") or ""),
            default_category=str(p.get("category") or "other"),
            description=str(p.get("description") or ""),
            mode=str(p.get("mode") or "new"),
            target_id=str(p.get("target_id") or ""),
            match_mode=str(p.get("match") or "auto"),
        )
        if r.get("ok"):
            self.logger.info(f"[词库] 上传「{r.get('name')}」{r.get('count')} 条（{r.get('mode')}，{r.get('id')}）")
        return json_response(r)

    async def web_lexicon_fetch_upstream(self):
        p = await request.json(default={})
        r = await self._fetch_upstream_lexicon(
            url=str(p.get("url") or ""),
            name=str(p.get("name") or ""),
            category=str(p.get("category") or ""),
            auto_enable=None if p.get("auto_enable") is None else bool(p.get("auto_enable")),
            mirror=None if p.get("mirror") is None else str(p.get("mirror") or ""),
            force_mirror=None if p.get("force_mirror") is None else bool(p.get("force_mirror")),
        )
        return json_response(r)

    async def web_lexicon_delete(self):
        p = await request.json(default={})
        r = self.lexicons.delete(str(p.get("id") or ""))
        if r.get("ok"):
            self.logger.info(f"[词库] 删除 {r.get('id')}")
        return json_response(r)

    async def web_lexicon_rename(self):
        p = await request.json(default={})
        r = self.lexicons.rename(str(p.get("id") or ""), str(p.get("name") or ""))
        return json_response(r)

    async def web_lexicon_export(self):
        return json_response(self.lexicons.export(str(request.query.get("id") or "")))

    async def web_lexicon_allow(self):
        p = await request.json(default={})
        action = str(p.get("action") or "add").lower()
        if action == "clear":
            self.logger.info("[词库] 清空白名单")
            return json_response(self.lexicons.allow_clear())
        raw = p.get("words")
        if isinstance(raw, str):
            items = [x.strip() for x in re.split(r"[\n,，、;；]+", raw) if x.strip()]
        elif isinstance(raw, list):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            items = []
        if not items:
            return json_response({"error": "没有有效的词"})
        if action == "remove":
            r = self.lexicons.allow_remove(items)
            self.logger.info(f"[词库] 白名单移除 {len(r.get('removed') or [])} 条")
            return json_response(r)
        r = self.lexicons.allow_add(items)
        self.logger.info(f"[词库] 白名单新增 {len(r.get('added') or [])} 条")
        return json_response(r)

    async def web_lexicon_test(self):
        p = await request.json(default={})
        text = str(p.get("text") or "")
        include_disabled = bool(p.get("include_disabled"))
        use_allow = p.get("use_allow", True) is not False
        hits = self.lexicons.hits(text, limit=20, include_disabled=include_disabled, use_allow=use_allow)
        allow_words = sorted(self.lexicons.allow_words()) if use_allow else []
        blocked_count = 0
        if use_allow and text:
            plain = self.lexicons.hits(text, limit=20, include_disabled=include_disabled, use_allow=False)
            blocked_count = len([h for h in plain if h["match"] in set(allow_words)])

        def _anon(h: dict) -> dict:
            """命中信息去词化：只报位置、词长、类型、来源，不回传词本身。"""
            return {
                "category": h.get("category"),
                "source": h.get("source"),
                "start": h.get("start"),
                "end": h.get("end"),
                "length": len(str(h.get("match") or "")),
                "is_regex": bool(h.get("is_regex")),
            }

        return json_response({
            "text": text,
            "count": len(hits),
            "hits": [_anon(h) for h in hits],
            "blocked_count": blocked_count,
            "word_display": "hidden",
            "stats": self.lexicons.stats(),
        })

    # ---------------- 入群自动审批策略（QQ 官方 /v2/groups/join_approval_strategy） ----------------
    def _pick_bot_pid(self, preferred: str = "") -> tuple[str, str]:
        """选一个平台实例 ID。preferred 命中则用之，否则取第一个启用的实例。

        返回 (pid, 错误信息)，成功时错误为空串。
        """
        plats = self._platform_list()
        if preferred:
            for pl in plats:
                if pl["id"] == preferred:
                    return pl["id"], ""
            return "", f"平台实例不存在：{preferred}"
        for pl in plats:
            if pl.get("enable"):
                return pl["id"], ""
        return "", "没有启用的 QQ 官方机器人实例，请先在 AstrBot 里配置并启用"

    def _approval_api(self, preferred: str = "") -> "QQApi":
        """取审批策略用的 API 客户端（策略是机器人级的，不绑群）。"""
        pid, err = self._pick_bot_pid(preferred)
        if err:
            raise QQApiError(err)
        try:
            api = self._get_api(pid)
        except QQApiError:
            raise
        except Exception as e:  # 面板接口不因实例异常直接 500
            raise QQApiError(f"取机器人实例失败：{e}") from e
        if api is None:
            raise QQApiError("取不到机器人实例的 API 客户端")
        return api

    @staticmethod
    def _parse_group_input(raw: str) -> list[str]:
        """把「123, 456 789」这类自由输入拆成干净列表（兼容中英文逗号、空格、换行）。"""
        if not raw:
            return []
        for sep in ("，", "、", ";", "；", "\n", "\t", "\r"):
            raw = raw.replace(sep, ",")
        return [x.strip() for x in raw.split(",") if x.strip()]

    async def web_join_approval_list(self):
        """GET 策略列表。策略按机器人维度，platform_id 可选。"""
        try:
            pid, perr = self._pick_bot_pid(request.query.get("platform_id", type=str) or "")
            if perr:
                return json_response({"error": perr})
            # 昵称是懒加载的，进审批页时顺手补一次，下拉里就能显示机器人名称而不是 AppID
            if not (self.mod.get_bot_meta(pid) or {}).get("name"):
                await self._refresh_bot_name(pid)
            api = self._get_api(pid)
            data = await api.list_join_approval_strategies(
                cursor=request.query.get("cursor", type=str) or "",
                limit=request.query.get("limit", type=int) or 20,
            )
        except QQApiError as e:
            return json_response({"error": str(e)})
        rows = []
        for s in data.get("strategies") or []:
            gids = [str(x) for x in (s.get("group_ids") or [])]
            gopens = [str(x) for x in (s.get("group_openids") or [])]
            expire_at = s.get("expire_at") or ""
            left_days = None
            try:
                if expire_at:
                    left_days = (datetime.fromisoformat(expire_at.replace("Z", "+00:00"))
                                 - datetime.now(timezone.utc)).days
            except Exception:
                left_days = None
            rows.append({
                "strategy_id": s.get("strategy_id") or "",
                "group_ids": gids,
                "group_openids": gopens,
                "group_mode": "id" if gids else "openid",
                "group_count": len(gids) + len(gopens),
                "whitelist_user_count": s.get("whitelist_user_count") or 0,
                "is_enable": (s.get("is_enable") or "").lower() == "on",
                "expire_at": fmt_rfc3339(expire_at),
                "expire_in_days": left_days,
                "created_at": fmt_rfc3339(s.get("created_at") or ""),
                "updated_at": fmt_rfc3339(s.get("updated_at") or ""),
                "remark": s.get("remark") or "",
            })
        return json_response({
            "platform_id": pid,
            "platforms": self._platform_list(),
            "strategies": rows,
            "next_cursor": data.get("next_cursor") or "",
        })

    async def web_join_approval_save(self):
        """POST 创建或修改策略。带 strategy_id＝修改，不带＝创建。"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        raw_groups = str(p.get("groups") or "")
        groups = self._parse_group_input(raw_groups)
        mode = str(p.get("group_mode") or "id")
        is_enable = "on" if p.get("is_enable", True) else "off"
        expire_at = str(p.get("expire_at") or "").strip()
        remark = str(p.get("remark") or "").strip()
        try:
            api = self._approval_api(str(p.get("platform_id") or ""))
            if sid:
                kw: dict = {"is_enable": is_enable}
                if expire_at:
                    kw["expire_at"] = expire_at
                kw["remark"] = remark
                r = await api.update_join_approval_strategy(sid, **kw)
                action = "已更新策略"
                op = str(p.get("group_op") or "").strip()
                if op in ("add", "del") and groups:
                    if mode == "id":
                        try:
                            act: dict = {"op": op, "group_ids": [int(x) for x in groups]}
                        except ValueError:
                            return json_response({"error": "群号必须是纯数字"})
                    else:
                        act = {"op": op, "group_openids": groups}
                    r = await api.update_join_approval_strategy(sid, group_action=act)
                    action = f"已更新策略，并{'新增' if op == 'add' else '移除'} {len(groups)} 个群"
            else:
                if not groups:
                    return json_response({"error": "至少要指定一个群"})
                if mode == "id":
                    try:
                        gids = [int(x) for x in groups]
                    except ValueError:
                        return json_response({"error": "群号必须是纯数字"})
                    r = await api.create_join_approval_strategy(
                        group_ids=gids, is_enable=is_enable,
                        expire_at=expire_at or None, remark=remark or None)
                else:
                    r = await api.create_join_approval_strategy(
                        group_openids=groups, is_enable=is_enable,
                        expire_at=expire_at or None, remark=remark or None)
                action = "已创建策略"
        except QQApiError as e:
            return json_response({"error": str(e)})
        self.logger.info(f"[入群审批] {action} {r.get('strategy_id') or sid}（{len(groups)} 群）")
        return json_response({"ok": True, "result": r, "message": action})

    async def web_join_approval_toggle(self):
        """POST 启停策略。"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        if not sid:
            return json_response({"error": "缺少 strategy_id"})
        on = bool(p.get("enabled"))
        try:
            api = self._approval_api(str(p.get("platform_id") or ""))
            r = await api.update_join_approval_strategy(sid, is_enable="on" if on else "off")
        except QQApiError as e:
            return json_response({"error": str(e)})
        self.logger.info(f"[入群审批] 策略 {sid} -> {'启用' if on else '停用'}")
        return json_response({"ok": True, "result": r})

    async def web_join_approval_delete(self):
        """POST 删除策略。"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        if not sid:
            return json_response({"error": "缺少 strategy_id"})
        try:
            api = self._approval_api(str(p.get("platform_id") or ""))
            await api.delete_join_approval_strategy(sid)
        except QQApiError as e:
            return json_response({"error": str(e)})
        self.logger.info(f"[入群审批] 已删除策略 {sid}")
        return json_response({"ok": True})

    async def web_join_approval_execute(self):
        """POST 立即全量扫描（异步，官方称约 10 分钟）。"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        if not sid:
            return json_response({"error": "缺少 strategy_id"})
        try:
            api = self._approval_api(str(p.get("platform_id") or ""))
            await api.execute_join_approval_strategy(sid)
        except QQApiError as e:
            return json_response({"error": str(e)})
        self.logger.info(f"[入群审批] 已触发全量扫描 {sid}")
        return json_response({"ok": True, "message": "已提交，官方异步执行，约 10 分钟完成"})

    async def web_join_approval_whitelist(self):
        """POST 增删白名单号码。body: strategy_id / op(add|del) / users(文本)"""
        p = await request.json(default={})
        sid = str(p.get("strategy_id") or "").strip()
        op = str(p.get("op") or "add")
        users = self._parse_group_input(str(p.get("users") or ""))
        if not sid:
            return json_response({"error": "缺少 strategy_id"})
        if not users:
            return json_response({"error": "白名单号码不能为空"})
        bad = [u for u in users if not u.isdigit()]
        if bad:
            return json_response({"error": f"以下不是纯数字 QQ 号：{', '.join(bad[:5])}"})
        try:
            api = self._approval_api(str(p.get("platform_id") or ""))
            r = await api.update_join_approval_whitelist(sid, op, users)
        except QQApiError as e:
            return json_response({"error": str(e)})
        self.logger.info(f"[入群审批] 白名单 {op} {len(users)} 个 -> {sid}")
        return json_response({"ok": True, "result": r,
                              "message": f"已{'添加' if op == 'add' else '移除'} {len(users)} 个号码"})

    def _register_web_apis(self) -> None:
        ctx = self.context
        ctx.register_web_api(f"/{WEB_API_PREFIX}/status", self.web_status, ["GET"], "QQ群管-状态总览")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mutes", self.web_mutes, ["GET"], "QQ群管-禁言列表")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/history", self.web_mute_history, ["GET"], "QQ群管-禁言历史")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/history/clear", self.web_mute_history_clear, ["POST"], "QQ群管-清空禁言历史")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/history/delete", self.web_mute_history_delete, ["POST"], "QQ群管-删除单条禁言历史")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/recalls", self.web_recalls, ["GET"], "QQ群管-撤回历史")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/recalls/clear", self.web_recalls_clear, ["POST"], "QQ群管-清空撤回历史")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/recalls/delete", self.web_recalls_delete, ["POST"], "QQ群管-删除单条撤回历史")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/requests", self.web_requests, ["GET"], "QQ群管-待审批列表")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/logs", self.web_logs, ["GET"], "QQ群管-日志查询")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/logs/stream", self.web_logs_stream, ["GET"], "QQ群管-日志SSE")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/logs/clear", self.web_logs_clear, ["POST"], "QQ群管-清空日志")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/poll", self.web_poll_now, ["POST"], "QQ群管-立即轮询")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/config", self.web_config_save, ["POST"], "QQ群管-保存默认禁言理由")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/config/schema", self.web_config_schema, ["GET"], "QQ群管-配置schema与当前值")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/config/save", self.web_config_bulk_save, ["POST"], "QQ群管-批量保存配置")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/templates/formats", self.web_tpl_formats, ["GET"], "QQ群管-消息模板格式")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/templates/formats/save", self.web_tpl_formats_save, ["POST"], "QQ群管-保存消息模板格式")
        # 群规审核（已并入管理面板 tab：pages/dashboard，路由前缀 mod/...）
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/groups", self.web_mod_groups, ["GET"], "审核-群列表")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/detail", self.web_mod_detail, ["GET"], "审核-单群详情(群规/设置/话术)")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/rules/save", self.web_mod_rules_save, ["POST"], "审核-保存群规")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/rules/transfer", self.web_mod_rules_transfer, ["POST"], "审核-转移群规到其他群")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/settings/save", self.web_mod_settings_save, ["POST"], "审核-保存举报设置")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/templates/save", self.web_mod_templates_save, ["POST"], "审核-保存话术模板")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts", self.web_mod_verdicts, ["GET"], "审核-判定记录")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts/export", self.web_mod_verdicts_export, ["GET"], "审核-导出判定记录")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts/delete", self.web_mod_verdicts_delete, ["POST"], "审核-删除单条判定记录")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts/clear", self.web_mod_verdicts_clear, ["POST"], "审核-清空判定记录")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts/revoke", self.web_mod_verdicts_revoke, ["POST"], "审核-撤销处罚")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts/prune", self.web_mod_verdicts_prune, ["POST"], "审核-按天清理判定记录")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/feedback", self.web_mod_feedback, ["POST"], "审核-误判标记")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/admins", self.web_mod_admins, ["GET"], "审核-提醒名单")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/admins/remove", self.web_mod_admins_remove, ["POST"], "审核-移出提醒名单")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/stats", self.web_mod_stats, ["GET"], "审核-统计")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/todo", self.web_mod_todo, ["GET"], "审核-待办队列")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/todo/handle", self.web_mod_todo_handle, ["POST"], "审核-处理待办")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/test", self.web_mod_test, ["POST"], "审核-试跑")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/group_rename", self.web_mod_group_rename, ["POST"], "审核-群备注名")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/group_names/refresh", self.web_mod_group_names_refresh, ["POST"], "审核-拉取群名")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/members", self.web_members, ["GET"], "成员档案-列表")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/members/stats", self.web_members_stats, ["GET"], "成员档案-统计")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/members/update", self.web_members_update, ["POST"], "成员档案-改昵称/备注")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/members/delete", self.web_members_delete, ["POST"], "成员档案-删除")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/members/clear_left", self.web_members_clear_left, ["POST"], "成员档案-清理已退群")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/members/import", self.web_members_import, ["POST"], "成员档案-批量导入")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/members/sync", self.web_members_sync, ["POST"], "成员档案-官方接口同步")
        # 词库（公开词库 + 上传 + 白名单 + 命中测试）
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons", self.web_lexicons, ["GET"], "词库-列表与统计")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/detail", self.web_lexicon_detail, ["GET"], "词库-详情与词条预览")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/toggle", self.web_lexicon_toggle, ["POST"], "词库-启用/停用")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/apply", self.web_lexicon_apply, ["POST"], "词库-批量启用")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/upload", self.web_lexicon_upload, ["POST"], "词库-上传/粘贴")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/fetch_upstream", self.web_lexicon_fetch_upstream, ["POST"], "词库-一键获取上游词库")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/delete", self.web_lexicon_delete, ["POST"], "词库-删除")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/rename", self.web_lexicon_rename, ["POST"], "词库-重命名")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/export", self.web_lexicon_export, ["GET"], "词库-导出")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/allow", self.web_lexicon_allow, ["POST"], "词库-白名单")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/lexicons/test", self.web_lexicon_test, ["POST"], "词库-命中测试")
        # 入群自动审批策略（QQ 官方 /v2/groups/join_approval_strategy）
        ctx.register_web_api(f"/{WEB_API_PREFIX}/approval/strategies", self.web_join_approval_list, ["GET"], "入群审批-策略列表")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/approval/save", self.web_join_approval_save, ["POST"], "入群审批-创建/修改策略")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/approval/toggle", self.web_join_approval_toggle, ["POST"], "入群审批-启停策略")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/approval/delete", self.web_join_approval_delete, ["POST"], "入群审批-删除策略")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/approval/execute", self.web_join_approval_execute, ["POST"], "入群审批-立即扫描")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/approval/whitelist", self.web_join_approval_whitelist, ["POST"], "入群审批-白名单增删")

    def _platform_list(self) -> list[dict]:
        out = []
        try:
            for p in self.context.platform_manager.platform_insts:
                meta = p.meta()
                if meta.name not in QQ_PLATFORM_NAMES:
                    continue
                cfg = getattr(p, "config", None) or {}
                appid = str(cfg.get("appid", ""))
                masked = appid
                if len(appid) > 8:
                    masked = f"{appid[:4]}****{appid[-4:]}"
                out.append({
                    "id": meta.id,
                    "appid": appid,
                    "appid_masked": masked,
                    "enable": bool(cfg.get("enable", True)),
                    "name": str(self.mod.get_bot_meta(meta.id).get("name") or ""),
                })
        except Exception as e:
            self.logger.error(f"枚举平台失败: {e}")
        return out

    async def web_status(self):
        # 面板上所有时间都按这个时区渲染（前端 chip / 脚注直接用这个值）
        timezone_name = self._refresh_timezone()
        groups = self._group_entries()
        return json_response({
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "timezone": timezone_name,
            "platforms": self._platform_list(),
            "features": {
                "enable_mute": bool(self._read_config_value(("basic", "enable_mute"), default=True, legacy_keys=("enable_mute",))),
                "enable_approval": bool(self._read_config_value(("approval", "enable_approval"), default=True, legacy_keys=("enable_approval",))),
                "enable_join_request_notify": bool(self._read_config_value(("approval", "enable_join_request_notify"), default=True, legacy_keys=("enable_join_request_notify",))),
                "enable_welcome": bool(self._read_config_value(("basic", "enable_welcome"), default=True, legacy_keys=("enable_welcome",))),
                "enable_group_add_intro": bool(self._read_config_value(("basic", "enable_group_add_intro"), default=True, legacy_keys=("enable_group_add_intro",))),
                "enable_new_group_welcome": bool(self._read_config_value(("basic", "enable_new_group_welcome"), default=True, legacy_keys=("enable_new_group_welcome",))),
                "enable_review": bool(self._read_config_value(("moderation", "enable"), default=True, legacy_keys=("moderation_enable",))),
                "enable_recall": bool(self._read_config_value(("recall", "enable_recall"), default=True, legacy_keys=("enable_recall",))),
                "enable_member_remove_notify": bool(self._read_config_value(("member_event", "enable_member_remove_notify"), default=False, legacy_keys=("enable_member_remove_notify",))),
                "enable_member_registry": bool(self._read_config_value(("member_registry", "enabled"), default=True, legacy_keys=("member_registry_enabled",))),
            },
            "admin": {
                "mode": str(self._read_config_value(("admin", "admin_mode"), default="any", legacy_keys=("admin_mode",))),
                "use_astrbot_admin": bool(self._read_config_value(("admin", "admin_use_astrbot_admin"), default=True, legacy_keys=("admin_use_astrbot_admin",))),
                "use_qq_role": bool(self._read_config_value(("admin", "admin_use_qq_role"), default=True, legacy_keys=("admin_use_qq_role",))),
                "extra_openids": self._read_config_value(("admin", "admin_openids"), default=[], legacy_keys=("admin_openids",)) or [],
            },
            "target_groups": self._read_config_value(("basic", "target_group_openids"), default=[], legacy_keys=("target_group_openids",)) or [],
            "known_groups": groups,
            "pending_count": sum(len(v) for v in self._pending.values()),
            "poll_interval": int(self._read_config_value(("approval", "poll_interval"), default=30, legacy_keys=("poll_interval",)) or 30),
            "poll_running": bool(self._poll_task and not self._poll_task.done()),
            # 1.4.0：申请获取方式（事件为主 / 纯轮询 / 仅事件）与按钮通道状态
            "approval": {
                "join_event_mode": self._join_event_mode(),
                "poll_interval_effective": self._poll_interval_seconds(),
                "poll_interval_fallback": int(self._read_config_value(("approval", "poll_interval_fallback"), default=300, legacy_keys=("poll_interval_fallback",)) or 300),
                "buttons": {
                    "enabled": bool(self._join_button_options()[0]),
                    "channel_ready": self._button_channel_ready(),
                    "permission": self._join_button_options()[1],
                    "max_requests": self._join_button_options()[2],
                },
                "join_event": {
                    "total": self._join_event_total,
                    "last_at": self._join_event_last,
                    "last_text": fmt_ts(self._join_event_last) if self._join_event_last else "",
                    # 有群收到过事件 = 事件通道确实在推；一个都没收到多半是
                    # 机器人不是群管理员（平台不推），或者注入后还没重连完成
                    "groups_seen": len(self._join_event_at),
                },
                "interaction": {
                    "total": self._interaction_total,
                    "last_at": self._interaction_last,
                    "last_text": fmt_ts(self._interaction_last) if self._interaction_last else "",
                    "last_result": self._interaction_last_result,
                },
            },
            "log": {
                "level": str(self._read_config_value(("log", "log_level"), default="DEBUG", legacy_keys=("log_level",))),
                "entries": self.logger.last_index + 1,
                "file": self.logger.file_path,
            },
        })

    def _resolve_group(self) -> tuple[str, str] | None:
        """从 query 解析 group_openid 与 platform_id。"""
        g = request.query.get("group_openid", type=str) or ""
        if not g:
            # 取第一个已知群
            for gg, pid in self._known_groups.items():
                return gg, pid
            return None
        pid = request.query.get("platform_id", type=str) or self._known_groups.get(g)
        if not pid:
            for pl in self._platform_list():
                if pl["enable"]:
                    pid = pl["id"]
                    break
        if not pid:
            return None
        return g, pid

    def _group_entries(self) -> list[dict]:
        """已知群列表（带群名 / 备注 / 归属机器人）。

        WebUI 需要「分机器人、分群」展示：同一个群在不同机器人下是两回事，
        所以这里把 platform_id → 机器人展示名也一并带上，前端下拉直接可用。
        """
        out: list[dict] = []
        for g, pid in self._known_groups.items():
            # 逐群容错：群名/备注/机器人标签任一取不到，都退回可用的最小信息，
            # 不能让一个坏条目把整个下拉框和 /requests 打成 500。
            name = alias = display = ""
            bot = ""
            try:
                meta = self.mod.get_group_meta(g) or {}
                name = str(meta.get("name") or "")
                alias = str(meta.get("alias") or "")
                display = self.mod.display_name(g) or ""
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"读取群 {g} 元数据失败（已跳过展示名）: {e}")
            try:
                bot = self._bot_label(pid).get("display") or pid or ""
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"读取群 {g} 的机器人信息失败: {e}")
                bot = pid or ""
            out.append({
                "group_openid": g,
                "platform_id": pid,
                "name": name,
                "alias": alias,
                "display": display or name or alias,
                "bot": bot,
            })
        return out

    def _pick_groups(self, wanted: str) -> tuple[list[dict], Optional[dict]]:
        """按 WebUI 传来的 group_openid 选出要查的群。

        wanted 为空或 all：返回全部已知群（聚合视角）；
        指定具体群：只返回该群（不在已知列表里时用 _resolve_group 兜一下）。
        第二个返回值是错误信息（群不存在 / 一个群都没有时）。
        """
        entries = self._group_entries()
        wanted = (wanted or "").strip()
        if wanted and wanted != "all":
            hit = [e for e in entries if e["group_openid"] == wanted]
            if hit:
                return hit, None
            resolved = self._resolve_group()
            if resolved and resolved[0] == wanted:
                return [{"group_openid": wanted, "platform_id": resolved[1],
                         "display": wanted[:12], "name": "", "alias": "", "bot": ""}], None
            return [], {"error": "unknown_group", "message": "该群尚未记录，请先在群内与机器人互动一次"}
        if entries:
            return entries, None
        resolved = self._resolve_group()
        if not resolved:
            return [], {"error": "no_group", "message": "尚未记录任何群，请先在群内与机器人互动"}
        gg, pid = resolved
        return [{"group_openid": gg, "platform_id": pid, "display": gg[:12],
                 "name": "", "alias": "", "bot": ""}], None

    @staticmethod
    def _mute_rule_summary(rule: dict) -> dict:
        """把官方返回的 global_rule 整理成「配置类型 + 是否真的生效 + 具体时段」。

        ⚠️ QQ 的 mode 只表示「配置过哪种禁言类型」，不是「当前是否在禁言」：
        定时禁言规则全部 enabled=false 之后，mode 仍会停在 "schedule" 不退回 "none"
        （官方响应示例里也是 mode=schedule 却带着 enabled=false 的规则）。
        所以必须结合每条规则的 enabled 才能判断当前到底有没有在禁言，否则面板会把
        「配过定时禁言但已停用」显示成正在禁言。
        """
        mode = str(rule.get("mode") or "").lower()
        windows: list[dict] = []
        for r in rule.get("recurring_rules") or []:
            if not isinstance(r, dict):
                continue
            days: list[int] = []
            for x in r.get("weekdays") or []:
                try:
                    d = int(x)
                except (TypeError, ValueError):
                    continue
                if 1 <= d <= 7:
                    days.append(d)
            windows.append({
                "kind": "recurring",
                "weekdays": days,
                "start": str(r.get("start_time") or ""),
                "end": str(r.get("end_time") or ""),
                "enabled": bool(r.get("enabled")),
            })
        for r in rule.get("schedule_rules") or []:
            if not isinstance(r, dict):
                continue
            windows.append({
                "kind": "once",
                "start_at": str(r.get("start_at") or ""),
                "end_at": str(r.get("end_at") or ""),
                "enabled": bool(r.get("enabled")),
            })
        active = True if mode == "always" else any(w["enabled"] for w in windows)
        return {"mode": mode, "active": active,
                "live": _mute_rule_live(mode, windows), "windows": windows}

    async def web_mutes(self):
        """GET /mutes?group_openid=xxx 当前禁言状态（实时查 QQ）。

        不传 group_openid（或传 all）＝把每个已知群都查一遍并带群名汇总；传具体群
        就只看那个群。多群 / 多机器人时前端靠 group_display / bot 字段区分。
        """
        sel, err = self._pick_groups(request.query.get("group_openid", type=str) or "")
        if err:
            return json_response(err)
        members: list[dict] = []
        errors: list[dict] = []
        group_rules: list[dict] = []
        for e in sel:
            g, pid = e["group_openid"], e["platform_id"]
            label = e.get("display") or g[:12]
            try:
                data = await self._get_api(pid).get_mute_state(g)
            except QQApiError as ex:
                errors.append({"group_openid": g, "group_display": label, "error": str(ex)})
                continue
            rule = data.get("global_rule") or {}
            summary = self._mute_rule_summary(rule)
            if summary["mode"] not in ("", "none"):
                group_rules.append({"group_openid": g, "group_display": label, **summary})
            for m in data.get("members") or []:
                row = dict(m)
                row["group_openid"] = g
                row["group_display"] = label
                row["bot"] = e.get("bot") or ""
                members.append(row)
        single = len(sel) == 1
        return json_response({
            "scope": "group" if single else "all",
            "group_openid": sel[0]["group_openid"] if single else "",
            "platform_id": sel[0]["platform_id"] if single else "",
            "group_display": (sel[0].get("display") or "") if single else "",
            "global_rule": group_rules[0] if (single and group_rules) else None,
            "groups": self._group_entries(),
            "selected": [e["group_openid"] for e in sel],
            "members": members,
            "group_rules": group_rules,
            "errors": errors,
        })

    async def web_mute_history(self):
        """GET /history?group_openid=xxx&limit=50 查询禁言历史（倒序，最新在前）。"""
        self._sweep_expired_mutes()  # 顺手把到期的 active 更新成 expired
        limit = request.query.get("limit", 50, type=int)
        limit = max(1, min(limit, 200))
        g = request.query.get("group_openid", type=str) or ""
        records: list[dict] = []
        groups = [g] if g else list(self._mute_history.keys())
        for gg in groups:
            records.extend(self._mute_history.get(gg, []))
        records.sort(key=lambda r: r.get("mute_at", 0), reverse=True)
        out = []
        for r in records[:limit]:
            out.append({
                "id": r.get("id"),
                "group_openid": r.get("group_openid"),
                "platform_id": r.get("platform_id"),
                "member_openid": r.get("member_openid"),
                "username": r.get("username", "未知成员"),
                "reason": r.get("reason", ""),
                "duration_text": r.get("duration_text", ""),
                "seconds": r.get("seconds", 0),
                "mute_at": r.get("mute_at", 0),
                "mute_at_text": fmt_ts(r.get("mute_at", 0)),
                "expire_at": r.get("expire_at", 0),
                "expire_at_text": fmt_ts(r.get("expire_at", 0)),
                "operator": r.get("operator", ""),
                "operator_name": r.get("operator_name", ""),
                "status": self._effective_mute_status(r),
                "unmute_at": r.get("unmute_at"),
                "unmute_at_text": fmt_ts(r.get("unmute_at", 0)) if r.get("unmute_at") else "",
            })
        return json_response({
            "total": len(records),
            "returned": len(out),
            "records": out,
        })

    async def web_mute_history_clear(self):
        """POST /history/clear body={'group_openid': 'xxx'} 清空历史（不传群则清空全部）。"""
        payload = await request.json(default={})
        g = payload.get("group_openid") or request.query.get("group_openid", type=str) or ""
        n = 0
        if g:
            n = len(self._mute_history.pop(g, []))
        else:
            n = sum(len(v) for v in self._mute_history.values())
            self._mute_history.clear()
        self._save_mute_history()
        self.logger.info(f"WebUI 清空禁言历史: {n} 条")
        return json_response({"cleared": n})

    async def web_mute_history_delete(self):
        """POST /history/delete body={'id': 123} 删除单条记录。"""
        payload = await request.json(default={})
        rid = payload.get("id")
        deleted = 0
        for g, lst in self._mute_history.items():
            for i, r in enumerate(lst):
                if r.get("id") == rid:
                    del lst[i]
                    deleted = 1
                    break
            if deleted:
                break
        if deleted:
            self._save_mute_history()
            self.logger.info(f"WebUI 删除禁言历史记录: id={rid}")
        return json_response({"deleted": deleted})

    async def web_recalls(self):
        """GET /recalls?group_openid=xxx&limit=50 撤回历史（不传群则全部群）。"""
        limit = request.query.get("limit", 100, type=int)
        limit = max(1, min(limit, 200))
        g = request.query.get("group_openid", type=str) or ""
        records: list[dict] = []
        if g:
            records = list(self._recall_history.get(g, []))
        else:
            for lst in self._recall_history.values():
                records.extend(lst)
        records.sort(key=lambda r: r.get("recalled_at", 0), reverse=True)
        out = []
        for r in records[:limit]:
            out.append({
                "id": r.get("id"),
                "group_openid": r.get("group_openid"),
                "platform_id": r.get("platform_id"),
                "message_id": r.get("message_id"),
                "content": r.get("content", ""),
                "source": r.get("source", ""),
                "recalled_at": r.get("recalled_at", 0),
                "recalled_at_text": fmt_ts(r.get("recalled_at", 0)),
                "operator": r.get("operator", ""),
                "operator_name": r.get("operator_name", ""),
            })
        return json_response({
            "total": len(records),
            "returned": len(out),
            "records": out,
        })

    async def web_recalls_clear(self):
        """POST /recalls/clear body={'group_openid': 'xxx'} 清空撤回历史（不传群则清空全部）。"""
        payload = await request.json(default={})
        g = payload.get("group_openid") or request.query.get("group_openid", type=str) or ""
        n = 0
        if g:
            n = len(self._recall_history.pop(g, []))
        else:
            n = sum(len(v) for v in self._recall_history.values())
            self._recall_history.clear()
        self._save_recall_history()
        self.logger.info(f"WebUI 清空撤回历史: {n} 条")
        return json_response({"cleared": n})

    async def web_recalls_delete(self):
        """POST /recalls/delete body={'id': 123} 删除单条撤回记录。"""
        payload = await request.json(default={})
        rid = payload.get("id")
        deleted = 0
        for g, lst in self._recall_history.items():
            for i, r in enumerate(lst):
                if r.get("id") == rid:
                    del lst[i]
                    deleted = 1
                    break
            if deleted:
                break
        if deleted:
            self._save_recall_history()
            self.logger.info(f"WebUI 删除撤回历史记录: id={rid}")
        return json_response({"deleted": deleted})

    async def web_requests(self):
        """GET /requests?group_openid=xxx 待处理入群申请。

        不传（或传 all）＝汇总全部已知群的申请（每条带所属群，便于分群查看）；
        传具体群＝只刷新并展示那一个群。_refresh_requests 内部失败会退回本地缓存。

        最外层再包一层兜底：面板这一块宁可显示「哪一步挂了」，也不要整块
        「加载失败: Internal server error」——500 一到前端就只剩一句通用报错，
        排查时完全看不出是哪一步炸的（历史现象：群选择器/群名元数据坏掉也会 500）。
        """
        try:
            return await self._web_requests_impl()
        except Exception as ex:  # noqa: BLE001
            import traceback

            tb = traceback.format_exc()
            self.logger.error(f"[/requests] 查询待审批列表失败: {ex}\n{tb}")
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

    async def _web_requests_impl(self):
        sel, err = self._pick_groups(request.query.get("group_openid", type=str) or "")
        if err:
            return json_response(err)
        rows: list[dict] = []
        errors: list[dict] = []
        for e in sel:
            g, pid = e["group_openid"], e["platform_id"]
            label = e.get("display") or g[:12]
            # 逐群容错：机器人下线 / 平台实例被删 / 单群接口报错，只跳过该群，
            # 不能把整页打成 500（历史 bug：一个失效群让「待处理申请」整块加载失败）
            try:
                pending = await self._refresh_requests(g, pid)
            except Exception as ex:  # noqa: BLE001
                self.logger.warning(f"群 {g} 拉取待审批申请失败，已跳过: {ex!s}")
                errors.append({
                    "group_openid": g,
                    "group_display": label,
                    "bot": e.get("bot") or "",
                    "error": str(ex),
                })
                continue
            for r in pending:
                rows.append({
                    "seq": len(rows) + 1,  # 汇总模式下跨群连续编号
                    "group_openid": g,
                    "group_display": label,
                    "bot": e.get("bot") or "",
                    "username": r.get("username"),
                    "member_openid": r.get("member_openid"),
                    "apply_at": fmt_rfc3339(r.get("apply_at", "")),
                    "apply_source": r.get("apply_source"),
                    "verify_info": r.get("verify_info"),
                    "risk_tips": r.get("risk_tips"),
                })
        single = len(sel) == 1
        return json_response({
            "scope": "group" if single else "all",
            "group_openid": sel[0]["group_openid"] if single else "",
            "platform_id": sel[0]["platform_id"] if single else "",
            "group_display": (sel[0].get("display") or "") if single else "",
            "groups": self._group_entries(),
            "selected": [e["group_openid"] for e in sel],
            "requests": rows,
            "errors": errors,
        })

    async def web_logs(self):
        level = request.query.get("level", type=str) or "ALL"
        keyword = request.query.get("keyword", type=str) or ""
        limit = request.query.get("limit", 200, type=int)
        return json_response({"logs": self.logger.search(level, keyword, limit),
                              **self.logger.log_status()})

    async def web_logs_stream(self):
        """SSE 日志流。

        没有新日志时必须定期发一行 SSE 注释（`: keepalive`）保活：
        否则连接会长时间零字节，被 Nginx / WebUI 反代按空闲超时断开，
        前端就表现为「断开几十秒」。注释行会被 EventSource 忽略，不影响数据。
        """
        async def gen():
            idx = self.logger.last_index
            idle = 0
            try:
                while True:
                    entries = self.logger.get_since(idx)
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

    async def web_logs_clear(self):
        """清空内存日志，并删除磁盘上的日志文件（真正释放空间）。"""
        n = self.logger.clear(remove_files=True)
        self.logger.info(f"WebUI 清空日志：内存 {n} 条 + 磁盘文件已删除")
        return json_response({"cleared": n, **self.logger.log_status()})

    async def web_poll_now(self):
        groups = self._managed_groups()
        results = {}
        for g, pid in groups.items():
            try:
                await self._poll_group(g, pid)
                results[g] = "ok"
            except Exception as e:
                results[g] = str(e)
        return json_response({"groups": results})

    async def web_config_save(self):
        """POST /config body={'default_mute_reason': 'xxx'} 保存默认禁言理由（即时生效）。"""
        payload = await request.json(default={})
        reason = str(payload.get("default_mute_reason", "")).strip()
        if not reason:
            return json_response({"ok": False, "message": "默认禁言理由不能为空"})
        if len(reason) > 50:
            return json_response({"ok": False, "message": "理由太长啦，50 字以内"})
        self._write_config_value("basic", "default_mute_reason", reason)
        try:
            self.config.save_config()
        except Exception as e:
            self.logger.warning(f"保存配置失败: {e}")
            return json_response({"ok": False, "message": f"配置写入失败: {e}"})
        self.logger.info(f"WebUI 更新默认禁言理由为: {reason}")
        return json_response({"ok": True, "default_mute_reason": reason})

    async def web_mod_verdicts_delete(self):
        """POST /mod/verdicts/delete body={'id': 12}

        只删本地这条判定留痕（省空间），不影响已经生效的禁言/撤回动作，
        也不会改变模型判定逻辑。
        """
        payload = await request.json(default={})
        try:
            vid = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return json_response({"ok": False, "message": "记录 id 无效"})
        if not vid:
            return json_response({"ok": False, "message": "缺少记录 id"})
        ok = self.mod.delete_verdict(vid)
        if ok:
            self.logger.info(f"WebUI 删除判定记录 #{vid}")
        return json_response({"ok": ok, "id": vid,
                              "message": "已删除该条判定记录" if ok else "没找到这条记录（可能已被删除）"})

    async def web_mod_verdicts_clear(self):
        """POST /mod/verdicts/clear body={'group': 'xxx'}

        不传 group 时清空全部群的判定记录，传则只清该群。仅删本地留痕。
        """
        payload = await request.json(default={})
        g = str(payload.get("group") or "").strip()
        removed = self.mod.clear_verdicts(g)
        self.logger.info(f"WebUI 清空判定记录：{removed} 条（{'群 ' + g if g else '全部群'}）")
        return json_response({"ok": True, "cleared": removed, "group": g,
                              "message": f"已清空 {removed} 条判定记录"})

    # ------------------------------------------------------------------
    # WebUI 设置页：配置 schema / 当前值 / 保存（与 AstrBot 配置双向同步）
    # ------------------------------------------------------------------
    def _conf_schema(self) -> dict:
        """读取插件配置 schema（_conf_schema.json），带缓存。

        设置页直接按这份 schema 渲染表单——它是 AstrBot 配置面板与插件本体
        共用的同一份定义，所以页面永远和 AstrBot 的配置项保持一致。
        """
        if self._schema_cache is not None:
            return self._schema_cache
        path = Path(__file__).with_name("_conf_schema.json")
        try:
            self._schema_cache = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            self.logger.warning(f"读取 _conf_schema.json 失败: {e}")
            self._schema_cache = {}
        return self._schema_cache

    def _write_config_value(self, group: str, key: str, value) -> None:
        """写入分组路径下的配置项。"""
        node = self.config.get(group)
        if not isinstance(node, dict):
            node = {}
            self.config[group] = node
        node[key] = value

    def _provider_options(self) -> list[dict]:
        """当前可用的 LLM Provider 列表（供审核模型下拉选择）。"""
        out: list[dict] = []
        try:
            for prov in self.context.get_all_providers():
                meta = prov.meta()
                out.append({
                    "id": meta.id,
                    "model": meta.model or meta.id,
                    "type": meta.type,
                    "display": f"{meta.model or meta.id}（{meta.type}）",
                })
        except Exception as e:
            self.logger.debug(f"获取 Provider 列表失败: {e}")
        return out

    @staticmethod
    def _coerce_config_value(item: dict, raw):
        """按 schema 类型转换配置值，返回 (value, error)。"""
        t = str(item.get("type") or "string")
        if t == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on", "y", "是", "开"), None
            return bool(raw), None
        if t in ("int", "float"):
            text = str(raw).strip()
            if text == "":
                return None, "不能为空"
            try:
                return (int(float(text)) if t == "int" else float(text)), None
            except Exception:
                return None, "需要填写数字"
        if t == "list":
            if isinstance(raw, list):
                items = [str(x).strip() for x in raw]
            else:
                items = [x.strip() for x in re.split(r"[\n,，;；\s]+", str(raw or ""))]
            return [x for x in items if x], None
        # string / text
        text = "" if raw is None else str(raw)
        options = item.get("options")
        if options and text and text not in options:
            return None, "只能是 " + " / ".join(str(o) for o in options)
        if len(text) > 8000:
            return None, "内容过长（限 8000 字）"
        return text, None

    async def web_tpl_formats(self):
        """GET /templates/formats：消息模板清单 + 各自配置的发送格式。"""
        schema = self._conf_schema()
        group = schema.get("templates") if isinstance(schema.get("templates"), dict) else {}
        items_schema = group.get("items") if isinstance(group.get("items"), dict) else {}
        templates = []
        for key, ischema in items_schema.items():
            if not isinstance(ischema, dict):
                continue
            meta = self._tpl_formats.get(key) or {}
            value = self._read_config_value(
                ("templates", key), default=ischema.get("default", ""), legacy_keys=(key,)
            )
            if key in moderation.DEFAULT_TEMPLATES:
                # 审核话术的实际生效值可能是面板设的全局话术，以它为准回显
                override = ((self.mod.templates or {}).get("global") or {}).get(key)
                if override:
                    value = override
            templates.append({
                "key": key,
                "description": str(ischema.get("description") or key),
                "hint": str(ischema.get("hint") or ""),
                "value": "" if value is None else str(value),
                "default": "" if ischema.get("default") is None else str(ischema.get("default")),
                "format": self._tpl_format(key),
                "url": str(meta.get("url") or ""),
                "width": int(meta.get("width") or 0),
                "height": int(meta.get("height") or 0),
            })
        return json_response({"ok": True, "kinds": list(self.TPL_FORMAT_KINDS), "templates": templates})

    async def web_tpl_formats_save(self):
        """POST /templates/formats/save：保存模板格式，可同时保存内容。

        body: {"items":[{"key":"msg_help","type":"markdown","url":"","width":0,"height":0,"value":"..."}]}
        兼容单项写法 {"key": ..., "type": ...}
        """
        payload = await request.json(default={}) or {}
        raw_items = payload.get("items")
        if isinstance(raw_items, dict):
            raw_items = [{"key": k, **(v if isinstance(v, dict) else {})} for k, v in raw_items.items()]
        if not isinstance(raw_items, list):
            raw_items = [payload]
        raw_items = [i for i in raw_items if isinstance(i, dict)]

        valid_keys = self._tpl_keys()
        saved: list[str] = []
        errors: list[str] = []
        content_changed = False
        for item in raw_items:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            if key not in valid_keys:
                errors.append(f"未知模板：{key}")
                continue
            meta = dict(self._tpl_formats.get(key) or {})
            if "type" in item:
                kind = str(item.get("type") or "text").strip().lower()
                meta["type"] = kind if kind in self.TPL_FORMAT_KINDS else "text"
            if "url" in item:
                meta["url"] = str(item.get("url") or "").strip()
            for dim in ("width", "height"):
                if dim in item:
                    try:
                        meta[dim] = max(0, int(item.get(dim) or 0))
                    except (TypeError, ValueError):
                        meta[dim] = 0
            self._tpl_formats[key] = meta
            if "value" in item:
                try:
                    value = str(item.get("value") or "")
                    self._write_config_value("templates", key, value)
                    if key in moderation.DEFAULT_TEMPLATES:
                        # 审核话术由 moderation 存储渲染（_mod_tpl），配置只是它的一个入口。
                        # 这里同步写一份到全局话术，否则「面板保存成功但群里没变」。
                        self.mod.set_templates("global", {key: value})
                    content_changed = True
                except Exception as e:
                    errors.append(f"{key} 内容保存失败: {e}")
            saved.append(key)

        if saved:
            try:
                self._save_tpl_formats()
            except Exception as e:
                return json_response({"ok": False, "message": f"格式写入失败: {e}"})
            if content_changed:
                try:
                    self.config.save_config()
                except Exception as e:
                    self.logger.warning(f"保存配置失败: {e}")
                    errors.append(f"内容写入配置文件失败: {e}")
        return json_response({"ok": bool(saved) and not errors, "saved": saved, "errors": errors})

    async def web_config_schema(self):
        """GET /config/schema：返回设置页所需的 schema、当前生效值与可选模型。"""
        schema = self._conf_schema()
        groups = []
        for gkey, gval in schema.items():
            if not isinstance(gval, dict) or not isinstance(gval.get("items"), dict):
                continue
            items = []
            for ikey, ival in gval["items"].items():
                if not isinstance(ival, dict):
                    continue
                value = self._read_config_value(
                    (gkey, ikey), default=ival.get("default"), legacy_keys=(ikey,)
                )
                items.append({
                    "key": ikey,
                    "description": str(ival.get("description") or ikey),
                    "type": str(ival.get("type") or "string"),
                    "default": ival.get("default"),
                    "options": ival.get("options") or [],
                    "hint": str(ival.get("hint") or ""),
                    "special": str(ival.get("_special") or ""),
                    "value": value,
                })
            groups.append({
                "key": gkey,
                "description": str(gval.get("description") or gkey),
                "items": items,
            })
        return json_response({
            "ok": True,
            "plugin": PLUGIN_NAME,
            "groups": groups,
            "providers": self._provider_options(),
            # 这些配置在插件加载时读取，改完需要重载插件才生效，页面会提示
            "reload_required": {"member_event": ["enable_member_remove_notify", "member_remove_try_fetch_name"]},
        })

    async def web_config_bulk_save(self):
        """POST /config/save：批量保存配置项并写入 AstrBot 配置文件。

        body: {"values": {"basic": {"enable_mute": true, ...}, ...}}
        兼容单字段写法 {"group": "basic", "key": "enable_mute", "value": true}
        """
        payload = await request.json(default={}) or {}
        schema = self._conf_schema()
        changes = payload.get("values") if isinstance(payload.get("values"), dict) else None
        if not changes and payload.get("group") and payload.get("key") is not None:
            changes = {str(payload["group"]): {str(payload["key"]): payload.get("value")}}
        if not isinstance(changes, dict) or not changes:
            return json_response({"ok": False, "message": "没有收到要保存的配置项"})

        applied: dict[str, dict] = {}
        errors: list[str] = []
        for gkey, kv in changes.items():
            gschema = schema.get(gkey)
            if not isinstance(gschema, dict) or not isinstance(gschema.get("items"), dict):
                errors.append(f"未知配置分组：{gkey}")
                continue
            if not isinstance(kv, dict):
                continue
            for ikey, raw in kv.items():
                ischema = gschema["items"].get(ikey)
                if not isinstance(ischema, dict):
                    errors.append(f"未知配置项：{gkey}.{ikey}")
                    continue
                value, err = self._coerce_config_value(ischema, raw)
                if err:
                    errors.append(f"{ischema.get('description') or ikey}：{err}")
                    continue
                self._write_config_value(gkey, ikey, value)
                # 清掉历史遗留的扁平同名 key，避免新旧两份值并存造成困惑
                if ikey in self.config and ikey != gkey:
                    try:
                        del self.config[ikey]
                    except Exception:
                        pass
                applied.setdefault(gkey, {})[ikey] = value

        if not applied:
            return json_response({"ok": False, "message": "；".join(errors[:3]) or "没有可保存的改动"})

        try:
            self.config.save_config()
        except Exception as e:
            self.logger.warning(f"保存配置失败: {e}")
            return json_response({"ok": False, "message": f"配置写入失败: {e}"})

        # 日志类配置支持热更新，不必重启插件
        if "log" in applied:
            log_cfg = applied["log"]
            try:
                self.logger.reconfigure(
                    level=log_cfg.get("log_level"),
                    to_file=log_cfg.get("log_to_file"),
                    max_entries=log_cfg.get("log_max_entries"),
                    keep_days=log_cfg.get("log_keep_days"),
                    log_dir=self._plugin_data_dir(),
                    plugin_name=PLUGIN_NAME,
                )
            except Exception as e:
                self.logger.warning(f"日志配置热更新失败: {e}")

        count = sum(len(v) for v in applied.values())
        detail = "、".join(f"{g}.{k}" for g, kv in applied.items() for k in kv)
        self.logger.info(f"WebUI 更新配置 {count} 项：{detail}")
        return json_response({
            "ok": True,
            "applied": applied,
            "errors": errors,
            "count": count,
            "message": f"已保存 {count} 项配置" + ("；" + "；".join(errors[:2]) if errors else ""),
        })
