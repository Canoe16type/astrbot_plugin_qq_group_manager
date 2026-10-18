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
import logging
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import At, Plain
# 只使用 astrbot.api 公开接口：不 import astrbot.core.* 内部实现。
# 面板「管理行为 → 指令」的禁用由 AstrBot 自己在 get_handlers_by_event_type()
# 里按 handler.enabled 过滤（见 star_handler.py），被禁用的指令根本不会回调到插件，
# 插件无需也无法插手 —— 早先为此去读 star_handlers_registry 属于私有 API 依赖，已移除。
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import json_response, request, stream_response

from .plugin_logger import PluginLogger
from .qq_api import QQApi, QQApiError
from . import member_event
from . import lexicon as lexicon_lib
from . import moderation
from . import variant as variant_lib

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
# 模块级 logger：初始化早期（self.logger 尚未构造，例如日志目录本身还没算出来）
# 就用它，AstrBot 的根 logger 会收进 data/logs，不要用 print 把信息扔到 stdout。
_BOOT_LOG = logging.getLogger(f"astrbot.plugin.{PLUGIN_NAME}")
WEB_API_PREFIX = PLUGIN_NAME
# 旧版数据目录名，仅用于迁移与清理兼容，禁止再用于新路径
LEGACY_PLUGIN_NAME = "qq_group_manager"

# ---- 一键获取上游词库 ----
LEXICON_UPSTREAM_NAME = "上游词库（一键获取）"
# 刻意不内置任何下载地址：词表由使用者自行寻找/自行提供，插件只负责「下载 + 解析 + 落地」。
LEXICON_UPSTREAM_URL = ""
LEXICON_UPSTREAM_MAX_BYTES = 2 * 1024 * 1024      # 单次下载上限 2 MB
LEXICON_UPSTREAM_TIMEOUT = 15.0                   # 下载超时（秒）
LEXICON_UPSTREAM_FAST_TIMEOUT = 8.0               # 配了加速地址时，直连只等这么久


class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向时复检目标地址，防止 302 跳内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        err = Main._check_url_public(newurl)
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
        import sys

        _BOOT_LOG.warning(
            f"botpy 引用字段 patch 失败（引用撤回可能不可用）: {e}"
        )


_patch_botpy_quote_fields()

DURATION_RE = re.compile(r"^\s*(\d+)\s*(秒|分钟|分|小时|时|天|s|m|h|d)?\s*$", re.I)

# 管理指令集合（需要管理员权限）
ADMIN_COMMANDS = ("/禁言", "/解禁", "/查禁言", "/禁言历史", "/审批", "/通过", "/拒绝", "/撤回", "/审核", "/群名", "/管理登记")

# 本插件注册到 AstrBot「管理行为 → 指令」面板的规范指令名（不带 /）。
# 说明：这些名字现在挂在指令组「群管」下作为子指令（面板里是 1 条组 + 子指令），
# 能搜索、能单独禁用、能改别名/改权限；真正的权限判定（AstrBot 管理员 /
# QQ 群角色 / 自定义 OpenID）仍由插件内 _is_admin 负责，比 AstrBot 的
# "仅管理员"更宽松。
REGISTERED_COMMANDS = (
    "禁言", "解禁", "查禁言", "禁言历史", "审批", "通过", "拒绝",
    "撤回", "审核", "群名", "管理登记", "群管", "举报", "群规",
)

# 禁言历史：每群最多保留的条数
HISTORY_MAX_PER_GROUP = 500


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
            max_entries=int(self._read_config_value(("log", "log_max_entries"), default=2000, legacy_keys=("log_max_entries",))),
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
        self._member_event_ok = False  # 群成员事件注入是否已成功
        self._last_inject_attempt = 0.0  # 最近一次注入尝试时间戳（防频繁重连）
        self._mute_history: dict[str, list[dict]] = {}  # group_openid -> [记录]
        self._history_seq = 0
        self._recall_history: dict[str, list[dict]] = {}  # group_openid -> [撤回记录]
        self._recall_seq = 0
        # 群规审核（/举报）：数据按群隔离、全部本地落盘；
        # 注意：只有收到 /举报 才会调用模型，平时不扫描、不判定、不执行。
        self.mod = moderation.ModerationStore(self._plugin_data_dir(), self.logger)
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
        self._mod_few_shot: str = "".join(
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
        self._initialized = False
        # 防重入：同一消息可能被多个正则 handler 命中（如 /禁言 与 /禁言历史），
        # 用消息 ID 在短时间内去重，避免重复执行导致重复回复。
        self._last_cmd_msg_id: str = ""
        self._last_cmd_ts: float = 0.0

        self._load_known_groups()
        self._load_intro_state()
        self._load_mute_history()
        self._load_recall_history()
        self._register_web_apis()
        self.logger.info(
            f"QQ 群管插件已加载 (log_level={self._read_config_value(('log', 'log_level'), default='DEBUG', legacy_keys=('log_level',))}, "
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
                _BOOT_LOG.info(f"数据目录已迁移：{LEGACY_PLUGIN_NAME} -> {PLUGIN_NAME}")
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
            with open(self._groups_file(), "w", encoding="utf-8") as f:
                json.dump(self._known_groups, f, ensure_ascii=False, indent=1)
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
            p = self._intro_state_path()
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"done": self._intro_done, "pending": self._intro_pending},
                          f, ensure_ascii=False, indent=1)
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
            with open(self._history_file(), "w", encoding="utf-8") as f:
                json.dump(
                    {"seq": self._history_seq, "records": self._mute_history},
                    f, ensure_ascii=False, indent=1,
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
            with open(self._recall_history_file(), "w", encoding="utf-8") as f:
                json.dump(
                    {"seq": self._recall_seq, "records": self._recall_history},
                    f, ensure_ascii=False, indent=1,
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
    def _render(self, key: str, **kw) -> str:
        tpl = str(self._read_config_value(("templates", key), default="", legacy_keys=(key,)))
        kw = {k: (str(v) if v is not None else "") for k, v in kw.items()}
        try:
            return tpl.format(**kw)
        except Exception:
            for k, v in kw.items():
                tpl = tpl.replace("{" + k + "}", v)
            return tpl

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        if not text:
            return
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception as e:
            self.logger.error(f"回复消息失败: {e}")

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
                if first not in ("禁言", "解禁", "查禁言", "禁言历史", "审批", "通过", "拒绝", "撤回", "群管", "举报", "群规", "审核", "群名", "管理登记"):
                    return
            await self._handle_command(event, text, forced_cmd=forced_cmd, skip=skip)
        except Exception as e:
            self.logger.error(f"处理群消息异常: {e}")
            import traceback

            self.logger.debug(traceback.format_exc())

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """普通群消息：仅做新群欢迎与轮询兜底，不处理指令。"""
        try:
            g = event.get_group_id()
            if g:
                sid = event.get_sender_id()
                sname = event.get_sender_name()
                if sid and sname:
                    self._nick_cache.setdefault(g, {})[sid] = sname
            self._ensure_poll_task()
            self._ensure_member_event_injected()  # 兜底：平台实例就绪后重试注入
            self._record_recent_msg(event)  # 引用撤回：原文匹配需要消息缓存
            await self._maybe_auto_scan(event)  # 审核：仅当「本群」开了巡检开关才会执行
            await self._maybe_welcome_new_group(event)
        except Exception as e:
            self.logger.error(f"处理群消息异常: {e}")

    # ------------------------------------------------------------------
    # 群成员事件（退群/被移出通知，运行时注入实现，见 member_event.py）
    # ------------------------------------------------------------------
    async def _on_group_member_remove(self, data: dict, platform_id: str | None = None) -> None:
        """成员退群/被移出：按配置在群内发言通知。

        Args:
            data: 网关事件体（含 group_openid / member_openid）。
            platform_id: 事件来源的 qq_official 实例 ID（member_event.py 注入
                时通过闭包传入），用于多实例下精确路由 API 客户端。
        """
        try:
            if not bool(self._read_config_value(
                ("member_event", "enable_member_remove_notify"),
                default=False,
                legacy_keys=("enable_member_remove_notify",),
            )):
                return
            g = data.get("group_openid") or ""
            member = data.get("member_openid") or ""
            if not g or not member:
                self.logger.warning(f"[成员事件] 退群事件缺少必要字段: {data}")
                return
            # 群过滤
            targets = self._read_config_value(
                ("basic", "target_group_openids"), default=[], legacy_keys=("target_group_openids",)
            ) or []
            if targets and g not in targets:
                return
            # 尝试点名
            username = ""
            if bool(self._read_config_value(
                ("member_event", "member_remove_try_fetch_name"),
                default=True,
                legacy_keys=("member_remove_try_fetch_name",),
            )):
                username = await self._resolve_member_name(g, member)
            text = self._render(
                "msg_member_remove_notify",
                username=username or "某位成员",
            )
            api = self._api_for_group_or_default(g, fallback_pid=platform_id)
            if api is None:
                self.logger.warning(f"[成员事件] 群 {g} 无可用 API 客户端，无法发送退群通知")
                return
            try:
                await api.send_group_message(g, text)
                self.logger.info(
                    f"[成员事件] 群 {g} 成员 {member}({username or '未知'}) 退群，已发送通知"
                )
            except Exception as e:
                self.logger.error(f"[成员事件] 发送退群通知失败（可能是主动消息频控）: {e}")
        except Exception as e:
            self.logger.exception(f"[成员事件] 处理退群事件异常: {e}")

    async def _resolve_member_name(self, group_openid: str, member_openid: str) -> str:
        """尽力获取成员昵称：内存缓存 -> 群成员接口。查不到返回空串。"""
        cached = (self._nick_cache.get(group_openid) or {}).get(member_openid)
        if cached:
            return cached
        api = self._api_for_group_or_default(group_openid)
        if api is not None:
            try:
                info = await api.get_group_member(group_openid, member_openid)
                name = (info or {}).get("nickname") or ""
                if name:
                    self._nick_cache.setdefault(group_openid, {})[member_openid] = name
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

    @qgm_group.command("禁言")
    async def g_mute(self, event: AstrMessageEvent):
        """禁言 @用户 时长 [理由]"""
        await self._run_command(event, forced_cmd="/禁言", skip=1)

    @qgm_group.command("解禁")
    async def g_unmute(self, event: AstrMessageEvent):
        """解除 @用户的禁言"""
        await self._run_command(event, forced_cmd="/解禁", skip=1)

    @qgm_group.command("查禁言")
    async def g_list_mutes(self, event: AstrMessageEvent):
        """查看本群当前禁言名单"""
        await self._run_command(event, forced_cmd="/查禁言", skip=1)

    @qgm_group.command("禁言历史")
    async def g_mute_history(self, event: AstrMessageEvent):
        """查看禁言记录（@用户 / 条数 / 汇总）"""
        await self._run_command(event, forced_cmd="/禁言历史", skip=1)

    @qgm_group.command("撤回")
    async def g_recall(self, event: AstrMessageEvent):
        """撤回消息（先引用目标消息再发）"""
        await self._run_command(event, forced_cmd="/撤回", skip=1)

    @qgm_group.command("审批")
    async def g_list_requests(self, event: AstrMessageEvent):
        """查看待处理的入群申请"""
        await self._run_command(event, forced_cmd="/审批", skip=1)

    @qgm_group.command("通过")
    async def g_approve(self, event: AstrMessageEvent):
        """通过入群申请（序号）"""
        await self._run_command(event, forced_cmd="/通过", skip=1)

    @qgm_group.command("拒绝")
    async def g_reject(self, event: AstrMessageEvent):
        """拒绝入群申请（序号 + 理由）"""
        await self._run_command(event, forced_cmd="/拒绝", skip=1)

    @qgm_group.command("审核")
    async def g_mod(self, event: AstrMessageEvent):
        """群规审核（状态 / 试运行 / 巡检 / 测试）"""
        await self._run_command(event, forced_cmd="/审核", skip=1)

    @qgm_group.command("群名")
    async def g_group_name(self, event: AstrMessageEvent):
        """刷新本群群名"""
        await self._run_command(event, forced_cmd="/群名", skip=1)

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
        self._ensure_member_event_injected()
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

        # 释放每个 QQApi 持有的 aiohttp.ClientSession。
        # 不做的话，每次重载/禁用插件都会泄漏一个连接池与若干 socket，
        # 反复重载会累积到文件描述符耗尽（aiohttp 退出时还会刷 Unclosed client session）。
        for api in list(self._apis.values()):
            try:
                await api.close()
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"关闭 HTTP 会话失败（忽略）: {e}")
        self._apis.clear()

    def _ensure_poll_task(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
            self.logger.debug("已启动入群申请轮询任务")

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

        if cmd == "/群管":
            await self._reply(event, self._render("msg_help", bot_name=event.get_platform_id()))
            return

        # /举报：所有群成员都能用（举报的意义就在于此），不受管理员门槛限制
        if cmd == "/举报":
            await self._cmd_report(event, parts)
            return

        # /群规：查看对所有人生效；增删改在 _cmd_rules 内部单独校验管理员
        if cmd == "/群规":
            await self._cmd_rules(event, parts)
            return

        if cmd not in ADMIN_COMMANDS:
            return  # 非本插件指令，不打扰

        # 群白名单检查
        targets = self._read_config_value(("basic", "target_group_openids"), default=[], legacy_keys=("target_group_openids",)) or []
        if targets and event.get_group_id() not in targets:
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
        if cmd == "/群名":
            await self._cmd_group_name(event)
            return
        if cmd == "/禁言":
            await self._cmd_mute(event, parts)
        elif cmd == "/禁言历史":
            await self._cmd_list_mute_history(event, parts)
        elif cmd == "/解禁":
            await self._cmd_unmute(event, parts)
        elif cmd == "/撤回":
            await self._cmd_recall(event, parts)
        elif cmd == "/查禁言":
            await self._cmd_list_mutes(event)
        elif cmd == "/审批":
            await self._cmd_list_requests(event)
        elif cmd in ("/通过", "/拒绝"):
            await self._cmd_review(event, cmd, parts)
        elif cmd == "/管理登记":
            await self._cmd_admin_register(event, parts)

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
                await self._reply(event, self._render("msg_unmute_success", username=f"<@{target}>"))
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
                    quoted_content = getattr(reply, "message_str", "") or ""
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
                    content = str(e0.get("content") or "")
                else:
                    mid = str(
                        getattr(e0, "msg_idx", "")
                        or getattr(e0, "id", "")
                        or getattr(e0, "message_id", "")
                    )
                    content = str(getattr(e0, "content", "") or "")
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
        api = self._get_api(platform_id)
        try:
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
        name = r.get("username") or r.get("member_openid", "?")
        t = fmt_rfc3339(r.get("apply_at", ""))
        vmsg = ""
        try:
            vi = r.get("verify_info") or {}
            if vi.get("method") == "verify_message" and vi.get("verify_message"):
                vmsg = f" 验证消息: {vi['verify_message']}"
        except Exception:
            pass
        source = "邀请" if r.get("apply_source") == "invited" else "主动"
        return f"{idx}. {name} ({source} {t}){vmsg}"

    async def _cmd_list_requests(self, event: AstrMessageEvent) -> None:
        g = event.get_group_id()
        pending = self._pending_list(g)
        if not pending:
            pending = await self._refresh_requests(g, event.get_platform_id())
        if not pending:
            await self._reply(event, self._render("msg_requests_empty"))
            return
        lines = [
            self._fmt_request_line(i + 1, r) for i, r in enumerate(pending)
        ]
        await self._reply(event, self._render("msg_requests_list", list="\n".join(lines)))

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
        join_request_id = target.get("join_request_id", "")
        username = target.get("username", member_openid)
        reason = " ".join(parts[2:]) if len(parts) > 2 else None

        api = self._get_api(event.get_platform_id())
        op = "approve" if cmd == "/通过" else "decline"
        try:
            await api.approve_join_request(
                g, member_openid, join_request_id,
                op=op, reject_reason=reason,
            )
        except QQApiError as e:
            self.logger.error(f"审批失败: {e}")
            await self._reply(event, self._render("msg_approve_failed", error=str(e)))
            return
        # 从缓存移除
        pending.pop(idx - 1)
        self._pending[g] = pending
        self.logger.info(f"审批完成: group={g} op={op} member={member_openid} reason={reason}")
        if op == "approve":
            await self._reply(
                event, self._render("msg_approve_success", username=username)
            )
            if bool(self._read_config_value(("basic", "enable_welcome"), default=True, legacy_keys=("enable_welcome",))):
                welcome = self._render(
                    "msg_welcome_after_approve",
                    username=username,
                    group_name=g,
                    bot_name=event.get_platform_id(),
                )
                try:
                    await api.send_group_message(g, welcome)
                    self.logger.info(f"已向群 {g} 推送欢迎语")
                except QQApiError as e:
                    self.logger.warning(
                        f"主动推送欢迎语失败（24h 内无人@机器人时 QQ 会限制主动消息）: {e}；"
                        f"欢迎语已附在回复中"
                    )
                    await self._reply(
                        event, f"{welcome}\n（主动推送受限，欢迎语如上）"
                    )
        else:
            await self._reply(
                event, self._render("msg_reject_success", username=username)
            )

    # ------------------------------------------------------------------
    # 入群申请轮询
    # ------------------------------------------------------------------
    def _managed_groups(self) -> dict[str, str]:
        """返回 {group_openid: platform_id} 轮询目标。"""
        targets = self._read_config_value(("basic", "target_group_openids"), default=[], legacy_keys=("target_group_openids",)) or []
        if targets:
            out = {}
            for g in targets:
                pid = self._known_groups.get(g)
                if pid:
                    out[g] = pid
                else:
                    self.logger.debug(f"管理名单中的群 {g} 尚未交互过，跳过轮询")
            return out
        return dict(self._known_groups)

    async def _poll_loop(self) -> None:
        self.logger.info("入群申请轮询任务启动")
        while True:
            try:
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
            interval = self._read_config_value(
                ("approval", "poll_interval"), default=30, legacy_keys=("poll_interval",)
            )
            try:
                interval = int(interval)
            except (TypeError, ValueError):
                interval = 30
            await asyncio.sleep(max(10, interval))

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
        self.logger.info(f"群 {group_openid} 发现 {len(new)} 个新入群申请")
        if not bool(self._read_config_value(("approval", "enable_join_request_notify"), default=True, legacy_keys=("enable_join_request_notify",))):
            return
        lines = []
        pending = self._pending[group_openid]
        for r in new:
            try:
                idx = pending.index(r) + 1
            except ValueError:
                idx = len(pending)
            lines.append(self._fmt_request_line(idx, r))
        text = self._render("msg_join_request_notify", list="\n".join(lines))
        api = self._get_api(platform_id)
        # 防重复推送：多个轮询任务并发时，同一申请短时间内（10 分钟）只推一次。
        # 注意：必须在 send 成功后才记录，失败可重试。
        now = time.time()
        notified = self._notified.setdefault(group_openid, {})
        fresh = [r for r in new if now - notified.get(self._req_key(r), 0) >= 600]
        if not fresh:
            self.logger.debug("新申请已在近期通知过，跳过重复推送")
            return
        try:
            await api.send_group_message(group_openid, text)
            self.logger.info(f"已推送入群申请通知到群 {group_openid}")
            for r in fresh:
                notified[self._req_key(r)] = now
        except QQApiError as e:
            self.logger.warning(f"推送申请通知失败: {e}（QQ 主动消息可能受 24h 互动限制）")

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
        """抽取消息里的图片地址（用于多模态审核）。"""
        urls: list[str] = []
        try:
            for c in (getattr(event.message_obj, "message", None) or []):
                if type(c).__name__ != "Image":
                    continue
                u = str(getattr(c, "url", "") or getattr(c, "file", "") or "")
                if u.startswith("http"):
                    urls.append(u)
        except Exception:
            pass
        return urls[:4]

    def _mod_settings(self, group: str) -> dict:
        return self.mod.get_settings(group)

    def _mod_tpl(self, group: str, key: str, **kw) -> str:
        """话术渲染：本群覆盖 -> 全局自定义 -> 内置默认。"""
        tpl = (self.mod.get_templates(group) or {}).get(key)
        if not tpl:
            tpl = moderation.DEFAULT_TEMPLATES.get(key, "")
        return moderation.render_tpl(tpl, **kw)

    def _find_recent_msg(self, group: str, text: str = "", ts: float = 0.0,
                         max_age: float = 3600.0) -> Optional[dict]:
        """在消息缓存里找被举报的那条（优先按原文，其次按时间邻近）。

        与撤回用的 _match_recent_msg_id 区别：这里要拿到**整条记录**
        （发送者 openid、图片、真实消息 ID），且时间窗更宽（审核不受 2 分钟限制）。
        """
        try:
            lst = self._recent_group_msgs.get(group) or []
            now = time.time()
            best, best_score = None, -1.0
            for r in lst:
                if now - float(r.get("ts") or 0) > max_age:
                    continue
                score = 0.0
                rt = (r.get("text") or "").strip()
                if text and rt == text.strip():
                    score += 10.0          # 原文完全一致最强
                elif text and rt and (text.strip() in rt or rt in text.strip()):
                    score += 4.0
                if ts:
                    score += max(0.0, 3.0 - abs(float(r.get("ts") or 0) - ts))
                if score > best_score:
                    best, best_score = r, score
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
                quoted_content = getattr(reply, "message_str", "") or ""
                for sub in (getattr(reply, "chain", None) or []):
                    if type(sub).__name__ == "Image":
                        u = str(getattr(sub, "url", "") or getattr(sub, "file", "") or "")
                        if u.startswith("http"):
                            quoted_images.append(u)
                    elif not quoted_content:
                        quoted_content = str(getattr(sub, "text", "") or "")
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
        if rec is None:
            out["text"] = quoted_content
            out["images"] = quoted_images
            return out
        real_id = str(rec.get("id") or "")
        # QQ 官方引用事件只给 REFIDX 引用索引，撤回 API 认的是缓存里的真实消息 ID
        if real_id and (not message_id or message_id.startswith(("REFIDX", "TMP_"))):
            message_id = real_id
        out.update({
            "resolved": True,
            "id": message_id or real_id,
            "text": quoted_content or str(rec.get("text") or ""),
            "sender": str(rec.get("sender") or ""),
            "sender_name": str(rec.get("sender_name") or ""),
            "images": list(rec.get("images") or []) or quoted_images,
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
                                     user=f"<@{target}>", count=count, days=win,
                                     duration=fmt_duration(int(info.get("mute_seconds") or 0)))
            else:
                text = self._mod_tpl(group, "mod_variant_strike_notify", mentions=mentions,
                                     user=f"<@{target}>", count=count, days=win, method=method)
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

    def _mod_review_fast(self, group: str, rep: dict, respect_switches: bool = True) -> Optional[dict]:
        """L0 关键词 + L1 缓存：零成本，命中直接出结果。

        respect_switches=False 时忽略「关键词层」开关（供 /审核 测试、页面试跑等诊断入口用）。
        """
        text = rep.get("text") or ""
        settings = self._mod_settings(group)
        hit = None
        if (not respect_switches) or settings.get("enable_keyword", True):
            hit = self._mod_keyword_hit(text, settings)
        if hit:
            cat = hit["category"]
            src = str(hit.get("source") or "")
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
            return cached
        return None

    async def _mod_review_llm(self, event: Optional[AstrMessageEvent], group: str, rep: dict,
                              context_lines: list[str], reporter_note: str = "",
                              pid_override: str = "", respect_switches: bool = True) -> Optional[dict]:
        """L2 语义 / L3 多模态：调模型拿 JSON 判定。返回 None 表示失败或该层已关闭。"""
        settings = self._mod_settings(group)
        if respect_switches and not settings.get("enable_model", True):
            return None  # 模型层已关闭：不发任何请求（省钱、降噪）
        # 费用熔断：超预算时不再调用模型（降级为仅关键词层）
        budget = float(settings.get("daily_budget") or 0)
        if budget > 0 and self.mod.stats(group).get("today_cost", 0) >= budget:
            self.logger.warning(f"[审核] 群 {group} 已达当日费用上限 {budget}，跳过模型判定")
            return None

        images = list(rep.get("images") or [])
        if respect_switches and not settings.get("enable_vision", True):
            images = []  # 图片层关闭：图片不进提示词，只判文字
        if not images and not (rep.get("text") or "").strip():
            return None  # 没有任何可判定内容（例如关闭图片层后只剩图片）
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

        async def _call(pid: str, with_images: list[str]):
            return await self.context.llm_generate(
                chat_provider_id=pid,
                system_prompt=sysp,
                prompt=up,
                image_urls=with_images or None,
                temperature=0.0,
                max_tokens=320,
            )

        pid = pid_override or await self._mod_provider_id(event, vision=bool(images), group=group)
        if not pid:
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
                self.logger.warning(f"[审核] 多模态判定失败，降级为纯文本: {e}")
        if resp is None:
            try:
                resp = await _call(pid, [])
            except Exception as e:
                self.logger.error(f"[审核] 模型调用失败: {e}")
                return None

        raw = getattr(resp, "completion_text", "") or ""
        verdict = moderation.parse_json_loose(raw)
        if not verdict:
            self.logger.warning(f"[审核] 模型未返回合法 JSON: {raw[:200]!r}")
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
        usage = getattr(resp, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else 0
        verdict["tokens"] = tokens
        price = float(self._read_config_value(("moderation", "price_per_1k_tokens"), default=0.0) or 0.0)
        verdict["cost"] = round(tokens / 1000.0 * price, 6)
        verdict["from_cache"] = False
        return verdict

    # ---------------- 执行（撤回 + 禁言） ----------------
    async def _mod_execute(self, event: AstrMessageEvent, group: str, rep: dict,
                           verdict: dict, settings: dict) -> dict:
        """按判定结果执行动作。返回执行摘要（含话术所需字段）。"""
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
            "dry_run": bool(settings.get("dry_run", True)),
            "cached": bool(verdict.get("from_cache")),
        }
        if not out["violation"]:
            return out
        # 置信度不足：不自动执行，只记录（避免误杀）
        try:
            thr = float(settings.get("confidence_threshold") or 0.75)
        except (TypeError, ValueError):
            thr = 0.75
        if float(out["confidence"] or 0) < thr and not out["cached"]:
            out["skipped_reason"] = f"置信度 {out['confidence']:.2f} < 阈值 {thr:.2f}"
            return out

        dec = moderation.decide_action(
            verdict,
            rules=self.mod.get_rules(group),
            max_seconds=int(settings.get("max_mute_seconds") or 86400),
            default_seconds=int(self._read_config_value(("moderation", "default_mute_seconds"), default=600) or 600),
        )
        out.update({
            "rule_name": dec.get("rule_name") or "",
            "seconds": int(dec.get("seconds") or 0),
            "duration": fmt_duration(int(dec.get("seconds") or 0)),
        })

        target = str(rep.get("sender") or "")
        if not target:
            out["skipped_reason"] = "无法确定被举报消息的发送者（消息过久未在缓存中）"
            return out
        # 豁免：管理员 / 白名单成员
        wl = [str(x) for x in (settings.get("whitelist") or [])]
        if target in wl or self._is_qq_admin_openid(group, target):
            out["skipped_reason"] = "该成员在豁免名单中"
            return out
        # 复犯累进（默认关）：按窗口内的前科次数把时长乘一个倍率。
        # 只延长时长、不改判定；没开启或没有前科时原样返回。
        out["prior_offenses"] = 0
        out["escalation"] = 1.0
        if settings.get("escalate_enabled", False):
            try:
                prior = self.mod.repeat_count(
                    group, target, window_days=int(settings.get("escalate_window_days") or 30)
                )
                if prior:
                    secs, mult = moderation.escalate_seconds(
                        int(out["seconds"] or 0), prior,
                        step=float(settings.get("escalate_step") or 1.0),
                        max_multiplier=float(settings.get("escalate_max_multiplier") or 4.0),
                        cap=int(settings.get("max_mute_seconds") or 86400),
                    )
                    out["prior_offenses"] = prior
                    out["escalation"] = mult
                    if secs != out["seconds"]:
                        self.logger.info(
                            f"[审核] 复犯累进：前科 {prior} 次，时长 {out['seconds']}s → {secs}s（×{mult}）"
                        )
                        out["seconds"] = secs
                        out["duration"] = fmt_duration(secs)
            except Exception as e:
                self.logger.warning(f"[审核] 复犯累进计算失败，按原时长执行：{e}")
        if out["dry_run"]:
            out["skipped_reason"] = "试运行模式，未实际执行"
            return out

        api = self._api_for_group_or_default(group, fallback_pid=event.get_platform_id())
        if api is None:
            out["skipped_reason"] = "无可用 API 客户端"
            return out

        # 1) 撤回
        # 官方规则（bot.q.qq.com「撤回群聊消息」）：
        #   a) 机器人是群管理员 -> 可撤回「普通群成员」的消息；
        #      是普通成员 -> 只能撤回自己发的消息（无权限时返回 40062003）；
        #   b) 无论是否管理员，「发送超过 2 分钟的消息不可撤回」（40064004），
        #      这是与身份无关的硬限制。
        # 所以默认只在 2 分钟内尝试；若想验证自己账号的实际权限，可把本群
        # recall_max_age_seconds 设为 0（= 不限，总是尝试，失败会记录原因）。
        mid = str(rep.get("id") or "")
        try:
            max_age = float(settings.get("recall_max_age_seconds", 110) or 0)
        except (TypeError, ValueError):
            max_age = 110.0
        age = time.time() - float(rep.get("ts") or 0)
        if mid and (max_age <= 0 or age <= max_age):
            try:
                await api.recall_group_message(group, mid)
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
            except Exception as e:
                out["recall_note"] = _recall_hint(e)
                self.logger.warning(f"[审核] 撤回失败（不影响禁言）: {out['recall_note']}")
        elif mid:
            out["recall_note"] = (f"消息已发出 {int(age)} 秒，超出官方 2 分钟撤回时限，"
                                  f"跳过撤回（可将本群 recall_max_age_seconds 设为 0 强制尝试）")
            self.logger.info(f"[审核] {out['recall_note']}")

        # 2) 禁言
        expire_ts = time.time() + int(out["seconds"])
        try:
            await api.set_mute(group, target, mute_expire_at=QQApi.format_rfc3339(expire_ts), op="add")
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
        targets = self._read_config_value(
            ("basic", "target_group_openids"), default=[], legacy_keys=("target_group_openids",)
        ) or []
        return (not targets) or (group in targets)

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
            "text": (rep.get("text") or "")[:200],
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
            "tokens": 0,
            "cost": 0.0,
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
            if not text:
                return
            rep = {
                "id": str(getattr(event.message_obj, "message_id", "") or ""),
                "text": text,
                "sender": event.get_sender_id() or "",
                "sender_name": event.get_sender_name() or "",
                "images": self._extract_images(event),
                "ts": time.time(),
            }
            # 变体疑似信号：与「是否违规」的判定并行、互不影响（variant_check 默认关，关着就不扫）
            variant = self._mod_variant_scan(g, rep, st)
            if variant:
                self.logger.info(
                    f"[审核][变体] 巡检疑似命中：method={variant['method']} "
                    f"strength={variant['strength']} hits={variant['count']} group={g}"
                )
            verdict = self._mod_review_fast(g, rep)
            if verdict is None and st.get("auto_scan_llm"):
                verdict = await self._mod_review_llm(
                    event, g, rep, self._mod_context_lines(g, rep, st), "（自动巡检）"
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
                "text": text[:200], "violation": result["violation"],
                "basis": result["basis"], "category": result["category"],
                "confidence": result["confidence"], "reason": result["reason"],
                "rule_name": result["rule_name"],
                "action": "mute" if result["executed"] else "none",
                "ban_seconds": result["seconds"], "skipped_reason": result["skipped_reason"],
                "executed": result["executed"], "dry_run": result["dry_run"],
                "recall_note": result.get("recall_note", ""),
                "from_cache": result["cached"],
                "severity": result.get("severity") or verdict.get("severity") or "",
                "prior_offenses": result.get("prior_offenses") or 0,
                "escalation": result.get("escalation") or 1.0,
                "model": verdict.get("model") or "keyword",
                "tokens": verdict.get("tokens") or 0,
                "cost": verdict.get("cost") or 0.0,
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
    async def _cmd_report(self, event: AstrMessageEvent, parts: list[str]) -> None:
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
        if not rep.get("resolved") or not (rep.get("text") or rep.get("images")):
            # 引用了但定位不到目标消息：绝不猜人，直接如实提示重新引用
            await self._reply(event, self._mod_tpl(g, "mod_quote_lost"))
            self.mod.add_verdict({
                "group": g, "reporter": reporter, "target": "", "text": "",
                "violation": False, "error": "unresolved_quote",
                "dry_run": bool(settings.get("dry_run")),
                "skipped_reason": "引用消息过旧或无法定位",
            })
            return
        mid = str(rep.get("id") or "")
        if mid and mid in self._mod_pending:
            return  # 同一条消息正在审核，忽略重复举报
        if mid:
            self._mod_pending.add(mid)
        note = " ".join(parts[1:])[:200].strip() if len(parts) > 1 else ""
        try:
            self.logger.info(f"[审核] 收到举报: group={g} reporter={reporter} text={rep.get('text','')[:60]!r}")
            # 变体检测（疑似信号，与「是否违规」的判定并行、互不影响）：
            # 命中只做 撤回 + 记录 + 进待办，绝不自动禁言。
            variant = self._mod_variant_scan(g, rep, settings)
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
                    "text": (rep.get("text") or "")[:200], "violation": False,
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
                "text": (rep.get("text") or "")[:200],
                "violation": result["violation"],
                "basis": result["basis"],
                "category": result["category"],
                "confidence": result["confidence"],
                "reason": result["reason"],
                "rule_name": result["rule_name"],
                "action": "mute" if result["executed"] else "none",
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
                "tokens": verdict.get("tokens") or 0,
                "cost": verdict.get("cost") or 0.0,
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
                await self._reply(event, self._mod_tpl(
                    g, "mod_dry_run",
                    basis=self.mod.get_dim_names(g).get(str(result["basis"]), str(result["basis"] or "")),
                    category=self.mod.get_cat_names(g).get(str(result["category"]), str(result["category"] or "")),
                    duration=result["duration"],
                ))
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
            await self._reply(event, self._mod_tpl(g, key, user=f"<@{sender}>", count=cnt))
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
                                               user=f"<@{sender}>", count=cnt))
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

        if sub in ("设置", "修改") :
            text = " ".join(parts[2:]).strip()
            if not text:
                await self._reply(event, "用法：/群管 群规 设置 规则名｜判定说明｜时长（多行用 ; 分隔）")
                return
            items = [x.strip() for x in re.split("[" + chr(10) + ";]", text) if x.strip()]
            new_rules = []
            for it in items:
                seg = [x.strip() for x in it.split("｜") if x.strip()]
                name = seg[0] if seg else "未命名规则"
                desc = seg[1] if len(seg) > 1 else name
                secs = parse_duration(seg[2]) if len(seg) > 2 else 3600
                new_rules.append({
                    "name": name, "desc": desc,
                    "category": "other", "severity": "medium",
                    "seconds": int(secs or 3600), "enabled": True,
                })
            for r in new_rules:
                self.mod.add_rule(g, r)
            await self._reply(event, f"已保存 {len(new_rules)} 条群规，当前共 {len(self.mod.get_rules(g))} 条。")
            return

        if sub == "添加":
            text = " ".join(parts[2:]).strip()
            if not text:
                await self._reply(event, "用法：/群管 群规 添加 规则名｜判定说明｜时长")
                return
            seg = [x.strip() for x in text.split("｜") if x.strip()]
            secs = parse_duration(seg[2]) if len(seg) > 2 else 3600
            rule = self.mod.add_rule(g, {
                "name": seg[0] if seg else "未命名规则",
                "desc": seg[1] if len(seg) > 1 else "",
                "category": "other", "severity": "medium",
                "seconds": int(secs or 3600), "enabled": True,
            })
            await self._reply(event, f"已添加群规：{rule['name']}（{rule['id']}）")
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

        await self._reply(event, "用法：/群管 群规 ｜ /群管 群规 添加 规则名｜说明｜时长 ｜ /群管 群规 删除 序号 ｜ /群管 群规 清空")

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
                    "发送失败。主动消息要求群内 24 小时内有人 @ 过机器人（11251），"
                    "先在本群 @ 我一下再试；入群事件那条路不受此限制。"
                )
            return
        if sub == "测试":
            text = " ".join(parts[2:]).strip()
            if not text:
                await self._reply(event, "用法：/群管 审核 测试 <消息内容>")
                return
            rep = {"text": text, "sender": "test", "sender_name": "测试用户", "images": [], "ts": time.time(), "id": ""}
            v = self._mod_review_fast(g, rep, respect_switches=False) or await self._mod_review_llm(
                event, g, rep, [], "（测试）", respect_switches=False)
            if not v:
                await self._reply(event, "测试失败：模型未返回结果。")
                return
            d = moderation.decide_action(v, rules=self.mod.get_rules(g),
                                        max_seconds=int(st.get("max_mute_seconds") or 86400))
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
                f"拟执行：{d.get('action')} {fmt_duration(int(d.get('seconds') or 0))}" + chr(10) +
                vh_text
            ))
            return
        # 默认：状态
        s = self.mod.stats(g)
        await self._reply(event, (
            f"【本群审核状态】" + chr(10) + ""
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

    async def _refresh_bot_name(self, platform_id: str) -> str:
        """拉机器人自己的昵称（GET /users/@me，普通接口），失败返回空串。"""
        try:
            api = self._get_api(platform_id)
        except QQApiError:
            return ""
        info = await api.get_bot_info()
        name = str((info or {}).get("username") or "").strip()
        if name:
            self.mod.set_bot_meta(platform_id, name=name[:40])
        return name

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
        target = next((v for v in self.mod.verdicts if int(v.get("id") or 0) == vid), None)
        if target is None:
            return json_response({"error": "not found"})
        target["feedback"] = label
        target["feedback_note"] = note
        self.mod._save(self.mod._verdict_path, self.mod.verdicts)
        # 误判样本回灌到 few-shot（只做提示，不自动改判定规则）
        if label == "false_positive" and target.get("text"):
            self._mod_few_shot += f"- 「{str(target['text'])[:80]}」→ violation=false（人工标注的误判样本，勿再判违规）" + chr(10) + ""
        return json_response({"saved": True})

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
                for v in self.mod.verdicts:
                    if int(v.get("id") or 0) == vid:
                        mid = str(v.get("msg_id") or "")
                        break
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
        if not g or not text:
            return json_response({"error": "missing group or text"})
        rep = {"text": text, "sender": "test", "sender_name": "测试用户", "images": [], "ts": time.time(), "id": ""}
        v = self._mod_review_fast(g, rep, respect_switches=False)
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
                                           respect_switches=False)
            used_llm = True
        if not v:
            return json_response({"error": "判定失败：模型未返回结果"})
        d = moderation.decide_action(v, rules=self.mod.get_rules(g),
                                    max_seconds=int(self._mod_settings(g).get("max_mute_seconds") or 86400))
        # 变体层也一起试跑：不看本群开关（页面测试就是要看效果），只借本群的阈值配置
        try:
            vopts = {**self._mod_settings(g), "variant_check": True}
            variant = self._mod_variant_scan(g, rep, vopts)
        except Exception:
            variant = None
        return json_response({
            "verdict": v,
            "action": d,
            "duration": fmt_duration(int(d.get("seconds") or 0)),
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
            if Main._ip_is_public(ip):
                return ""
            return "不抓取本机 / 内网地址"
        # 域名：解析结果全部为公网才放行（解析失败就交给 urlopen 报错）
        try:
            for info in socket.getaddrinfo(host, None):
                addr = info[4][0]
                if not Main._ip_is_public(ipaddress.ip_address(addr)):
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
        err = Main._check_url_public(url)
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
        # 群规审核（已并入管理面板 tab：pages/dashboard，路由前缀 mod/...）
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/groups", self.web_mod_groups, ["GET"], "审核-群列表")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/detail", self.web_mod_detail, ["GET"], "审核-单群详情(群规/设置/话术)")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/rules/save", self.web_mod_rules_save, ["POST"], "审核-保存群规")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/settings/save", self.web_mod_settings_save, ["POST"], "审核-保存举报设置")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/templates/save", self.web_mod_templates_save, ["POST"], "审核-保存话术模板")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts", self.web_mod_verdicts, ["GET"], "审核-判定记录")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts/delete", self.web_mod_verdicts_delete, ["POST"], "审核-删除单条判定记录")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/verdicts/clear", self.web_mod_verdicts_clear, ["POST"], "审核-清空判定记录")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/feedback", self.web_mod_feedback, ["POST"], "审核-误判标记")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/admins", self.web_mod_admins, ["GET"], "审核-提醒名单")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/admins/remove", self.web_mod_admins_remove, ["POST"], "审核-移出提醒名单")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/stats", self.web_mod_stats, ["GET"], "审核-统计")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/todo", self.web_mod_todo, ["GET"], "审核-待办队列")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/todo/handle", self.web_mod_todo_handle, ["POST"], "审核-处理待办")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/test", self.web_mod_test, ["POST"], "审核-试跑")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/group_rename", self.web_mod_group_rename, ["POST"], "审核-群备注名")
        ctx.register_web_api(f"/{WEB_API_PREFIX}/mod/group_names/refresh", self.web_mod_group_names_refresh, ["POST"], "审核-拉取群名")
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

    def _platform_list(self) -> list[dict]:
        out = []
        try:
            for p in self.context.platform_manager.platform_insts:
                meta = p.meta()
                if meta.name != "qq_official":
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
                "enable_new_group_welcome": bool(self._read_config_value(("basic", "enable_new_group_welcome"), default=True, legacy_keys=("enable_new_group_welcome",))),
                "default_mute_reason": str(self._read_config_value(("basic", "default_mute_reason"), default="违反群规", legacy_keys=("default_mute_reason",))),
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
            meta = self.mod.get_group_meta(g)
            out.append({
                "group_openid": g,
                "platform_id": pid,
                "name": str(meta.get("name") or ""),
                "alias": str(meta.get("alias") or ""),
                "display": self.mod.display_name(g),
                "bot": self._bot_label(pid).get("display") or pid,
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
        """
        sel, err = self._pick_groups(request.query.get("group_openid", type=str) or "")
        if err:
            return json_response(err)
        rows: list[dict] = []
        for e in sel:
            g, pid = e["group_openid"], e["platform_id"]
            label = e.get("display") or g[:12]
            pending = await self._refresh_requests(g, pid)
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
        })

    async def web_logs(self):
        level = request.query.get("level", type=str) or "ALL"
        keyword = request.query.get("keyword", type=str) or ""
        limit = request.query.get("limit", 200, type=int)
        return json_response({"logs": self.logger.search(level, keyword, limit),
                              **self.logger.log_status()})

    async def web_logs_stream(self):
        async def gen():
            idx = self.logger.last_index
            try:
                while True:
                    entries = self.logger.get_since(idx)
                    for e in entries:
                        yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                        idx = e["index"]
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
        desc = str(item.get("description") or item.get("hint") or "该项")
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
