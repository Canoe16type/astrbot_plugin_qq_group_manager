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
import functools
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

from qgm.kernel.template_defaults import TEMPLATE_DEFAULTS

from .plugin_logger import LOG_CONTEXT, PluginLogger
from .qgm.kernel.storage import write_json_atomic as _write_json_atomic
from .qgm.kernel.text import short_id
from .qgm.features.audit import policy as audit_policy
from .qgm.features.audit import record as audit_record
from .qgm.features.audit import service as audit_service
from .qgm.features.audit import review as audit_review
from .qgm.features.audit import revoke as audit_revoke
from .qgm.features.audit import web as audit_web
from .qgm.features.audit import rules as audit_rules
from .qgm.features.audit.rules import RULE_USAGE_ADD, RULE_USAGE_SET, parse_rule_spec
from .qgm.features.lexicon import web as lexicon_web
from .qgm.features.status import web as status_web
from .qgm.features.members import web as members_web
from .qgm.features.config import web as config_web
from .qgm.commands import router as command_router
from .qgm.commands import parse as command_parse
from .qgm.features.mute import commands as mute_commands
from .qgm.features.status import commands as status_commands
from .qgm.features.members import commands as members_commands
from .qgm.features.kick import commands as kick_commands
from .qgm.features.kick import web as kick_web
from .qgm.features.members import cache as members_cache
from .qgm.features.members import names as members_names
from .qgm.features.audit import commands as audit_commands
from .qgm.features.audit import engine as audit_engine
from .qgm.features.audit import quotes as audit_quotes
from .qgm.features.audit import media as audit_media
from .qgm.features.audit import helpers as audit_helpers
from .qgm.setup import init_plugin
from .qgm.features.lexicon import upstream as lexicon_upstream
from .qgm.features.status import report as status_report
from .qgm.features.status import service as status_service
from .qgm.commands import runtime as command_runtime
from .qgm.features.audit import scan as audit_scan
from .qgm.features.admin import purge as admin_purge
from .qgm.features.approval import buttons as approval_buttons
from .qgm.features.approval import service as approval_service
from .qgm.web import registry as web_registry
from .qgm.kernel.constants import WEB_API_PREFIX
from .qgm.features.status import proactive as status_proactive
from .qgm.features.admin import commands as admin_commands
from .qgm.features.approval import commands as approval_commands
from .qgm.features.approval import web as approval_web
from .qgm.features.mute import web as mute_web
from .qgm.features.status.web import LOG_SSE_HEARTBEAT_SEC
from .qgm.kernel.timefmt import (
    _active_tz, active_timezone_name, fmt_duration, fmt_rfc3339, fmt_ts, parse_duration,
    set_active_timezone,
)
from .qgm.kernel.constants import (ADMIN_COMMANDS, LEXICON_UPSTREAM_FAST_TIMEOUT, LEXICON_UPSTREAM_MAX_BYTES, LEXICON_UPSTREAM_NAME,
    LEXICON_UPSTREAM_TIMEOUT, LEXICON_UPSTREAM_URL, PLUGIN_NAME, PLUGIN_VERSION,
    REGISTERED_COMMANDS)
from .qgm.kernel.text import _PURGE_LABELS, _purge_summary_text, _recall_hint, _refresh_hint
from .qgm.features.members import events as member_events
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
# 日志出口统一走 astrbot.api.logger（AstrBot 会按插件路由到 astrbot.plugin.<id>，
# 收进 data/logs）。初始化早期（self.logger / PluginLogger 还没构造完）也用它，
# 插件不得使用 stdlib logging，出口只能是 astrbot.api.logger。
# 腾讯 QQ 官方机器人的两种接入形态（都是同一套开放平台 API、同样的 appid/secret）：
#   - qq_official：AstrBot 内置 WebSocket 适配器（botpy），成员事件注入只对它有效；
#   - qq_official_webhook：4.16 起提供的 Webhook 适配器，事件由平台推到本地 HTTP。
# 面板/API 只关心「有哪些机器人实例、appid 是什么」，所以两种都要认；
# 否则用 Webhook 模式的人打开面板会看到「没有机器人」，所有页签都是空的。
QQ_PLATFORM_NAMES = ("qq_official", "qq_official_webhook")
# 旧版数据目录名，仅用于迁移与清理兼容，禁止再用于新路径
LEGACY_PLUGIN_NAME = "qq_group_manager"

# ---- 一键获取上游词库 ----
# 刻意不内置任何下载地址：词表由使用者自行寻找/自行提供，插件只负责「下载 + 解析 + 落地」。

class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向时复检目标地址，防止 302 跳内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        err = QQGroupManager._check_url_public(newurl)
        if err:
            raise ValueError(f"重定向到不安全的地址（{err}）")
        return super().redirect_request(req, fp, code, msg, headers, newurl)





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
                # noqa: silent-cleanup —— 降级兜底，不影响主流程
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


# 管理指令集合（需要管理员权限）
# 管理员指令：新的「大类」+ 旧的独立写法都列在这里（旧写法会先被 _normalize_aliases 折进大类）

# 本插件注册到 AstrBot「管理行为 → 指令」面板的规范指令名（不带 /）。
# 说明：这些名字现在挂在指令组「群管」下作为子指令（面板里是 1 条组 + 子指令），
# 能搜索、能单独禁用、能改别名/改权限；真正的权限判定（AstrBot 管理员 /
# QQ 群角色 / 自定义 OpenID）仍由插件内 _is_admin 负责，比 AstrBot 的
# "仅管理员"更宽松。
# 1.5.0 起对外只推「大类」：禁言 / 审批 / 状态 / 审核 / 群规 / 撤回 / 举报 / 成员 / 管理登记。
# 旧的独立指令（解禁、查禁言、禁言历史、通过、拒绝、汇报、体检、自检、群名）
# 仍作为别名挂在对应大类下，老写法继续可用。
# 禁言历史：每群最多保留的条数
HISTORY_MAX_PER_GROUP = 500


# 退群清理：各类数据的可读名（日志 / 回执里给用户看）



# 中文数字映射（"一分钟" -> 1）


# 群规「一行式」写法里的分隔符：全角竖线 / 半角竖线 / 兼容 U+FE31 竖排形式。
# 教训：以前只认全角「｜」，用户用半角「|」时整句会被当成规则名，
# 说明和时长静默丢失（群里表现为「已添加群规：侮辱」）。



# 面板与消息里的时间统一按 AstrBot 全局配置 timezone 渲染（默认 Asia/Shanghai / UTC+8），
# 不跟随宿主机或浏览器时区；时长类配置一律以「秒」为单位，只在展示时换算。




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



def _with_log_context(func):
    """自动把「实例/群/成员」写入本协程内所有日志（contextvars 传播）。

    这样不用逐个改 200+ 个 logger 调用点，也能保证日志都带上下文；
    手动已经带了 `_ctx()` 的日志不会重复加前缀（PluginLogger 会判断）。
    """
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        ctx = ""
        try:
            first = args[0] if args else (kwargs.get("event") or kwargs.get("data"))
            if hasattr(first, "get_platform_id"):
                ctx = self._ctx(first.get_platform_id() or "", first.get_group_id() or "",
                                first.get_sender_id() or "")
            elif isinstance(first, dict):
                platform = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
                ctx = self._ctx(platform, first.get("group_openid") or "", first.get("member_openid") or "")
        except Exception:
            ctx = ""
        token = LOG_CONTEXT.set(ctx)
        try:
            return await func(self, *args, **kwargs)
        finally:
            LOG_CONTEXT.reset(token)
    return wrapper


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
            # noqa: silent-cleanup —— 降级兜底，不影响主流程
            pass

    def __init__(self, context, config=None) -> None:
        """装配：见 qgm/setup.py。"""
        return init_plugin(self, context, config)

    # 路径 / 持久化
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
        except Exception as e:
            logger.warning(f"数据目录迁移失败（继续使用新目录）：{e}")

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

    async def _try_fetch_group_name(self, group: str, pid: str):
        """薄壳：见 qgm/features/members/names.py。"""
        return await members_names._try_fetch_group_name(self, group, pid)

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

    async def _check_group_presence(self, group: str, pid: str=''):
        """薄壳：见 qgm/features/members/names.py。"""
        return await members_names._check_group_presence(self, group, pid)

    def _purge_group_data(self, group, pid='', reason='') -> dict:
        """薄壳：见 qgm/features/admin/purge.py。"""
        return admin_purge._purge_group_data(self, group, pid, reason)

    async def _cmd_status(self, event: AstrMessageEvent, parts: list[str] | None=None):
        """薄壳：见 status_commands。"""
        return await status_commands._cmd_status(self, event, parts)

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
        except Exception as e:
            self.logger.debug(f"[权限] 读取 AstrBot 管理员列表失败，回退到插件配置：{e}")
        extra = self._read_config_value(("admin", "admin_openids"), default=[],
                                        legacy_keys=("admin_openids",)) or []
        return sid in extra

    async def _revoke_verdict(self, group: str, vid: int, *, operator: str = "",
                              operator_name: str = "", reason: str = "",
                              is_bot_admin: bool = False) -> dict:
        """薄壳：撤销逻辑见 qgm.features.audit.revoke。"""
        return await audit_revoke.revoke_verdict(
            self, group, vid, operator=operator, operator_name=operator_name,
            reason=reason, is_bot_admin=is_bot_admin)

    @staticmethod
    def _is_punishable(v: dict) -> bool:
        """这条记录是否属于「可撤销的处罚」。"""
        return (bool(v.get("violation"))
                and (bool(v.get("executed")) or str(v.get("action") or "") in ("warn", "recall", "mute"))
                and not v.get("revoked"))

    def _fmt_verdict_line(self, v: dict) -> str:
        """单行摘要；实现见 qgm.features.audit.record。"""
        return audit_record.fmt_verdict_line(
            v, self.mod.get_cat_names(str(v.get("group") or "")), fmt_duration)

    @_with_log_context
    async def _cmd_revoke(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """薄壳：撤销逻辑见 qgm.features.audit.revoke。"""
        return await audit_revoke.cmd_revoke(self, event, parts)

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

    async def _cmd_selfcheck(self, event: AstrMessageEvent, parts: list[str] | None=None):
        """薄壳：见 status_commands。"""
        return await status_commands._cmd_selfcheck(self, event, parts)

    async def _cmd_presence(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 members_commands。"""
        return await members_commands._cmd_presence(self, event, parts)

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

    def _load_report_state(self):
        """薄壳：见 qgm/features/status/service.py。"""
        return status_service._load_report_state(self)

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
                           operator: str, operator_name: str = "",
                           event_id: str = "", target_openid: str = "",
                           target_name: str = "") -> dict:
        """记录一条撤回操作。content 为被撤回消息的原文（引用撤回时能拿到，
        直接传 message_id 时平台接口不返回内容，则留空仅保留消息 ID）。"""
        self._recall_seq += 1
        rec = {
            "id": self._recall_seq,
            "group_openid": group_openid,
            "platform_id": platform_id,
            "message_id": message_id,
            "event_id": event_id or "",
            "target_openid": target_openid or "",
            "target_name": target_name or "",
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
            except Exception as e:
                self.logger.debug(f"[权限] 读取 AstrBot admins_id 失败，回退到事件角色：{e}")
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
    TPL_FALLBACKS = TEMPLATE_DEFAULTS

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
        if not meta and key in ("msg_mod_warn", "msg_mod_revoke"):
            # 两条都带 @：纯文本通道不解析 <qqbot-at-user>，必须走 markdown
            return "markdown"
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
            # 已经是 @ 标签（<qqbot-at-user ...> / <@...>）就不要二次包装
            kw["at"] = target if target.startswith("<") else self._at_tag(target)
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
    _AT_TAG_RE = re.compile(r'<qqbot-at-user id="([^"]+)"\s*/>')

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

    def _official_avatar_url(self, event, openid: str) -> str:
        """从 QQ 官方群消息事件里取头像 URL（author.avatar / mentions[].avatar）。

        官方 2026-11 起在消息事件下发头像链接；取不到时返回空串，
        调用方回退到旧的 qlogo 拼接（兼容尚未透传的 AstrBot 版本）。
        """
        if event is None or not openid:
            return ""
        try:
            raw = getattr(event.message_obj, "raw_message", None)
        except Exception:
            return ""
        if raw is None:
            return ""

        def _avatar_of(u):
            if isinstance(u, dict):
                oid = str(u.get("member_openid") or u.get("id") or "")
                av = u.get("avatar") or ""
            else:
                oid = str(getattr(u, "member_openid", "") or getattr(u, "id", "") or "")
                av = getattr(u, "avatar", "") or ""
            return oid, str(av)

        author = raw.get("author") if isinstance(raw, dict) else getattr(raw, "author", None)
        if author is not None:
            oid, av = _avatar_of(author)
            if av and (not oid or oid == openid):
                return av
        mentions = raw.get("mentions") if isinstance(raw, dict) else getattr(raw, "mentions", None)
        for u in mentions or []:
            oid, av = _avatar_of(u)
            if av and oid == openid:
                return av
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
                if not openid or fmt != "markdown":
                    return ""
                try:
                    disp = int(m.group(1)) if m.group(1) else 100
                except ValueError:
                    disp = 100
                disp = max(24, min(disp, 320))
                # 官方事件优先提供头像 URL；旧适配器没有该字段时回退 qlogo 拼接
                url = self._official_avatar_url(event, openid)
                if not url:
                    if not appid:
                        return ""
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

    def _plain_at_name(self, event, openid: str, group: str = "") -> str:
        """纯文本模式下的 @ 显示：优先成员档案群昵称/昵称，其次发送者名，最后后 6 位。"""
        name = ""
        try:
            rec = self.members.get(group, openid) if group else None
            if rec:
                name = str(rec.get("group_nickname") or rec.get("nickname") or "").strip()
        except Exception:
            name = ""
        if not name and event is not None:
            try:
                if str(event.get_sender_id() or "") == openid:
                    name = str(event.get_sender_name() or "").strip()
            except Exception:
                name = ""
        return "@" + (name or str(openid)[-6:])

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
        if fmt != "markdown" and content:
            group = ""
            try:
                group = str(event.get_group_id() or "") if event is not None else ""
            except Exception:
                group = ""
            content = self._AT_TAG_RE.sub(
                lambda m: self._plain_at_name(event, m.group(1), group), content)
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

    async def _send_group_proactive(self, group_openid, text, platform_id, *, at_openid='', keyboard=None):
        """薄壳：见 status_proactive。"""
        return await status_proactive._send_group_proactive(self, group_openid, text, platform_id, at_openid=at_openid, keyboard=keyboard)

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

    def _load_recent_msgs(self):
        """薄壳：见 qgm/features/members/cache.py。"""
        return members_cache._load_recent_msgs(self)

    def _save_recent_msgs(self, force: bool=False):
        """薄壳：见 qgm/features/members/cache.py。"""
        return members_cache._save_recent_msgs(self, force)

    def _record_recent_msg(self, event: AstrMessageEvent):
        """薄壳：见 qgm/features/members/cache.py。"""
        return members_cache._record_recent_msg(self, event)

    def _match_recent_msg_id(self, group_openid: str, text: str, max_age: float=150.0):
        """薄壳：见 qgm/features/members/cache.py。"""
        return members_cache._match_recent_msg_id(self, group_openid, text, max_age)

    async def _run_command(self, event, forced_cmd=None, skip=0):
        """薄壳：见 qgm/commands/runtime.py。"""
        return await command_runtime._run_command(self, event, forced_cmd, skip)

    # 按钮点击必须走独立的 button_action handler：AstrBot 唤醒阶段对按钮事件只放行挂了
    # ButtonActionFilter 的 handler，普通 event_message_type 的 handler 会被直接跳过。
    # （踩过的坑：写在 on_group_message 里时，点击只改了客户端按钮状态，服务端一次没执行。）
    if _BUTTON_ACTION_SUPPORTED:

        @_BUTTON_ACTION_DECORATOR()
        @_with_log_context
        async def on_button_action(self, event: AstrMessageEvent):
            """按钮点击：入群审批的「通过 / 拒绝」"""
            await self._consume_button_action(event)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @_with_log_context
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
                # noqa: silent-cleanup —— 降级兜底，不影响主流程
                pass
            return True
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"处理按钮点击异常: {e}")
            import traceback

            self.logger.debug(traceback.format_exc())
            return False

    async def _track_group_activity(self, event: AstrMessageEvent):
        """薄壳：见 qgm/features/status/service.py。"""
        return await status_service._track_group_activity(self, event)

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

    def _record_member_seen(self, event: AstrMessageEvent):
        """薄壳：见 qgm/features/members/cache.py。"""
        return members_cache._record_member_seen(self, event)

    # 入群欢迎去重窗口（秒）：同一事件被重推、或审批通过时已经欢迎过，不再重复发。
    JOIN_WELCOME_DEDUPE_SECONDS = 600.0

    @staticmethod
    def _member_event_key(group: str, member: str, data: dict) -> str:
        """事件去重键：群 + 成员 + 平台 event_id。

        平台会**跨群复用同一个 event_id**（实测两个群的退群事件 id 完全相同），
        只按 event_id 去重会把别的群的正常事件当重复丢掉。
        """
        base = str(data.get("_event_id") or data.get("timestamp") or "")
        return f"{group}|{member}|{base}"

    def _member_event_seen(self, event_id: str):
        """薄壳：见 qgm/features/members/cache.py。"""
        return members_cache._member_event_seen(self, event_id)

    @_with_log_context
    async def _on_group_member_add(self, data: dict, platform_id: str | None = None) -> None:
        """薄壳：成员事件见 qgm.features.members.events。"""
        return await member_events.on_member_add(self, data, platform_id)

    async def _welcome_member_on_join(self, group_openid, member_openid, platform_id):
        """薄壳：见 member_events。"""
        return await member_events._welcome_member_on_join(self, group_openid, member_openid, platform_id)

    def _claim_join_welcome(self, group_openid: str, member_openid: str):
        """薄壳：见 qgm/features/approval/service.py。"""
        return approval_service._claim_join_welcome(self, group_openid, member_openid)

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

    def _prune_join_welcomed(self, now: float | None=None):
        """薄壳：见 qgm/features/approval/service.py。"""
        return approval_service._prune_join_welcomed(self, now)

    @_with_log_context
    async def _on_group_member_remove(self, data: dict, platform_id: str | None = None) -> None:
        """薄壳：成员事件见 qgm.features.members.events。"""
        return await member_events.on_member_remove(self, data, platform_id)

    async def _resolve_member_name(self, group_openid: str, member_openid: str):
        """薄壳：见 qgm/features/members/names.py。"""
        return await members_names._resolve_member_name(self, group_openid, member_openid)

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
    # 指令布局：所有指令统一收进指令组「群管」，WebUI「管理行为 → 指令」里
    # 只有一条「群管」，展开是 14 个子指令 —— 面板清爽，也不会有重复条目。
    # 写法：/群管 禁言 @用户 10分钟、/群管 撤回 …；单独发 /群管 自动列出指令树。
    # 全部子指令共用 _run_command（skip=1 丢掉组名与子指令名），权限逻辑不变。
    # ------------------------------------------------------------------
    @filter.command_group("群管")
    def qgm_group(self):
        """群管指令组：禁言 / 撤回 / 审批 / 群规审核 / 词库相关操作"""

    @qgm_group.command("帮助")
    @_with_log_context
    async def g_help(self, event: AstrMessageEvent):
        """查看本插件完整帮助（比自动指令树更详细）"""
        await self._run_command(event, forced_cmd="/群管", skip=1)

    @qgm_group.command("禁言")
    @_with_log_context
    async def g_mute(self, event: AstrMessageEvent):
        """禁言 @用户 时长 [理由]｜禁言 解除 @用户｜禁言 名单｜禁言 历史"""
        await self._run_command(event, forced_cmd="/禁言", skip=1)

    @qgm_group.command("踢人")
    @_with_log_context
    async def g_kick(self, event: AstrMessageEvent):
        """踢人 [拉黑] @成员... [理由]｜踢人 记录 [N]（官方内邀能力）"""
        await self._run_command(event, forced_cmd="/踢人", skip=1)

    @qgm_group.command("撤回")
    @_with_log_context
    async def g_recall(self, event: AstrMessageEvent):
        """撤回消息（先引用目标消息再发）"""
        await self._run_command(event, forced_cmd="/撤回", skip=1)

    @qgm_group.command("审批")
    @_with_log_context
    async def g_list_requests(self, event: AstrMessageEvent):
        """待处理入群申请｜审批 通过 N｜审批 拒绝 N [理由]"""
        await self._run_command(event, forced_cmd="/审批", skip=1)

    @qgm_group.command("撤销")
    @_with_log_context
    async def g_revoke(self, event: AstrMessageEvent):
        """撤销一条处罚记录并致歉：/群管 撤销 <记录ID> [理由]"""
        await self._run_command(event, forced_cmd="/撤销", skip=1)

    @qgm_group.command("状态")
    @_with_log_context
    async def g_status(self, event: AstrMessageEvent):
        """日报（默认）｜状态 群｜状态 自检｜状态 群名"""
        await self._run_command(event, forced_cmd="/状态", skip=1)

    @qgm_group.command("审核")
    @_with_log_context
    async def g_mod(self, event: AstrMessageEvent):
        """群规审核（状态 / 试运行 / 巡检 / 测试）"""
        await self._run_command(event, forced_cmd="/审核", skip=1)

    @qgm_group.command("管理登记")
    @_with_log_context
    async def g_admin_register(self, event: AstrMessageEvent):
        """审核提醒名单（列表 / 取消）"""
        await self._run_command(event, forced_cmd="/管理登记", skip=1)

    @qgm_group.command("举报")
    @_with_log_context
    async def g_report(self, event: AstrMessageEvent):
        """引用违规消息后举报（所有人可用）"""
        await self._run_command(event, forced_cmd="/举报", skip=1)

    @qgm_group.command("群规")
    @_with_log_context
    async def g_rules(self, event: AstrMessageEvent):
        """查看 / 配置本群群规"""
        await self._run_command(event, forced_cmd="/群规", skip=1)

    @qgm_group.command("成员")
    @_with_log_context
    async def g_members(self, event: AstrMessageEvent):
        """群成员档案（列表 / 改名 / 备注 / 删除 / 拉取昵称）"""
        await self._run_command(event, forced_cmd="/成员", skip=1)

    @qgm_group.command("记录")
    @_with_log_context
    async def g_records(self, event: AstrMessageEvent):
        """插件处理记录（全量 / 我的 / 详情 / 汇总）"""
        await self._run_command(event, forced_cmd="/记录", skip=1)

    async def _cmd_records(self, event: AstrMessageEvent, parts: list[str]):
        from qgm.features.records import commands as records_commands
        return await records_commands.cmd_records(self, event, parts)

    def _ensure_member_event_injected(self):
        """薄壳：见 qgm/features/members/cache.py。"""
        return members_cache._ensure_member_event_injected(self)

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
                # noqa: silent-cleanup —— 清理/权限尽力而为，失败不影响主流程
                pass

        # 定时汇报任务同理：不取消的话重载后会和新实例一起发，一天变两条
        rtask = self._report_task
        self._report_task = None
        if rtask and not rtask.done():
            rtask.cancel()
            try:
                await rtask
            except (asyncio.CancelledError, Exception):
                # noqa: silent-cleanup —— 清理/权限尽力而为，失败不影响主流程
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
            # noqa: silent-cleanup —— 清理/权限尽力而为，失败不影响主流程
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
    def _report_options(self):
        """薄壳：见 qgm/features/status/service.py。"""
        return status_service._report_options(self)

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

    def _report_candidates(self, target: str):
        """薄壳：见 qgm/features/status/service.py。"""
        return status_service._report_candidates(self, target)

    def _ensure_report_task(self):
        """薄壳：见 qgm/features/status/service.py。"""
        return status_service._ensure_report_task(self)

    async def _report_loop(self):
        """薄壳：见 qgm/features/status/service.py。"""
        return await status_service._report_loop(self)

    def _local_now(self):
        tz = _active_tz()
        return datetime.now(tz) if tz else datetime.now()

    async def _maybe_send_report(self):
        """薄壳：见 qgm/features/status/report.py。"""
        return await status_report.maybe_send_report(self)

    async def _build_report_text(self, group_openid: str, platform_id: str, *, pending: list[dict] | None=None):
        """薄壳：见 qgm/features/status/service.py。"""
        return await status_service._build_report_text(self, group_openid, platform_id, pending=pending)

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
    async def _handle_command(self, event, text, forced_cmd=None, skip=0):
        """薄壳：见 qgm/commands/runtime.py。"""
        return await command_runtime._handle_command(self, event, text, forced_cmd, skip)

    # ------------------------------------------------------------------
    # /成员：群成员档案
    # ------------------------------------------------------------------
    async def web_kick_history(self):
        """薄壳：踢人记录见 qgm.features.kick.web。"""
        return await kick_web.web_kick_history(self)

    async def _cmd_kick(self, event: AstrMessageEvent, parts: list[str] | None = None):
        """薄壳：踢人见 qgm.features.kick.commands。"""
        return await kick_commands.cmd_kick(self, event, parts)

    async def _cmd_member(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 members_commands。"""
        return await members_commands._cmd_member(self, event, parts)

    def _member_ref(self, group_openid: str, token: str):
        """薄壳：见 qgm/features/members/names.py。"""
        return members_names._member_ref(self, group_openid, token)

    def _render_member_list(self, group_openid: str, *, status: str='all', keyword: str='', page: int=1, title: str=''):
        """薄壳：见 qgm/features/members/names.py。"""
        return members_names._render_member_list(self, group_openid, status=status, keyword=keyword, page=page, title=title)

    async def _cmd_member_sync(self, group_openid: str):
        """薄壳：见 members_commands。"""
        return await members_commands._cmd_member_sync(self, group_openid)

    async def _cmd_group_name(self, event: AstrMessageEvent):
        """薄壳：见 members_commands。"""
        return await members_commands._cmd_group_name(self, event)

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
        except Exception as e:
            self.logger.debug(f"[权限] 读取平台管理员标记失败：{e}")
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
        except Exception as e:
            self.logger.debug(f"[权限] 枚举机器人管理员失败：{e}")
        return out

    async def _cmd_mute(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 mute_commands。"""
        return await mute_commands._cmd_mute(self, event, parts)

    async def _cmd_unmute(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 mute_commands。"""
        return await mute_commands._cmd_unmute(self, event, parts)

    async def _cmd_recall(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 mute_commands。"""
        return await mute_commands._cmd_recall(self, event, parts)

    def _extract_quote_from_raw(self, raw):
        """薄壳：见 qgm/features/audit/quotes.py。"""
        return audit_quotes._extract_quote_from_raw(self, raw)

    async def _cmd_list_mutes(self, event: AstrMessageEvent, parts: list[str] | None = None):
        """薄壳：见 mute_commands。"""
        return await mute_commands._cmd_list_mutes(self, event, parts)

    @staticmethod
    def _mute_status_text(status: str) -> str:
        return {"active": "禁言中", "unmuted": "已解禁", "expired": "已到期"}.get(status, "")

    def _aggregate_member_stats(self, records: list[dict]):
        """薄壳：见 qgm/features/members/names.py。"""
        return members_names._aggregate_member_stats(self, records)

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

    async def _cmd_list_mute_history(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 mute_commands。"""
        return await mute_commands._cmd_list_mute_history(self, event, parts)

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

    async def _refresh_requests(self, group_openid: str, platform_id: str):
        """薄壳：见 qgm/features/approval/service.py。"""
        return await approval_service._refresh_requests(self, group_openid, platform_id)

    def _fmt_request_line(self, idx: int, r: dict) -> str:
        return f"{idx}. {self._request_brief(r)}"

    # 待处理列表每页条数（被动回复，一条消息塞太多会被平台截断）
    REQUEST_PAGE_SIZE = 10

    @staticmethod
    def _request_brief(r: dict):
        """薄壳：见 qgm/features/approval/service.py。"""
        return approval_service._request_brief(r)

    async def _cmd_list_requests(self, event: AstrMessageEvent, parts: list[str] | None=None):
        """薄壳：见 approval_commands。"""
        return await approval_commands._cmd_list_requests(self, event, parts)

    async def _list_requests_all_groups(self, event: AstrMessageEvent, page: int):
        """薄壳：见 qgm/features/approval/service.py。"""
        return await approval_service._list_requests_all_groups(self, event, page)

    @_with_log_context
    async def _cmd_report(self, event: AstrMessageEvent, parts: list[str] | None=None):
        """薄壳：见 status_commands。"""
        return await status_commands._cmd_report(self, event, parts)

    async def _cmd_review(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 approval_commands。"""
        return await approval_commands._cmd_review(self, event, parts)

    async def _apply_join_decision(self, event: Optional[AstrMessageEvent], group_openid: str, platform_id: str, req: dict, op: str, *, reason: str | None=None, via: str='指令'):
        """薄壳：见 qgm/features/approval/service.py。"""
        return await approval_service._apply_join_decision(self, event, group_openid, platform_id, req, op, reason=reason, via=via)

    async def _welcome_after_approve(self, event: Optional[AstrMessageEvent], group_openid: str, platform_id: str, member_openid: str, username: str):
        """薄壳：见 qgm/features/approval/service.py。"""
        return await approval_service._welcome_after_approve(self, event, group_openid, platform_id, member_openid, username)

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

    async def _poll_loop(self):
        """薄壳：见 qgm/features/status/service.py。"""
        return await status_service._poll_loop(self)

    def _poll_interval_seconds(self):
        """薄壳：见 qgm/features/status/service.py。"""
        return status_service._poll_interval_seconds(self)

    async def _poll_group(self, group_openid: str, platform_id: str):
        """薄壳：见 qgm/features/status/service.py。"""
        return await status_service._poll_group(self, group_openid, platform_id)

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

    def _join_button_options(self):
        """薄壳：见 qgm/features/approval/service.py。"""
        return approval_service._join_button_options(self)

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

    async def _on_group_join_request(self, data, platform_id):
        """薄壳：见 member_events。"""
        return await member_events._on_group_join_request(self, data, platform_id)

    JOIN_NOTIFY_DEBOUNCE_SECONDS = 2.0

    def _schedule_join_notify(self, group_openid: str, platform_id: str | None):
        """薄壳：见 qgm/features/approval/service.py。"""
        return approval_service._schedule_join_notify(self, group_openid, platform_id)

    async def _flush_join_notify(self, group_openid: str, platform_id: str | None):
        """薄壳：见 qgm/features/approval/service.py。"""
        return await approval_service._flush_join_notify(self, group_openid, platform_id)

    async def _notify_join_requests(self, group_openid, platform_id, reqs, *, source='轮询'):
        """薄壳：见 member_events。"""
        return await member_events._notify_join_requests(self, group_openid, platform_id, reqs, source=source)

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

    async def _handle_button_interaction(self, event):
        """薄壳：见 qgm/features/approval/buttons.py。"""
        return await approval_buttons._handle_button_interaction(self, event)

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
    def _extract_images(self, event: AstrMessageEvent):
        """薄壳：见 qgm/features/audit/media.py。"""
        return audit_media._extract_images(self, event)

    def _mod_settings(self, group: str):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return audit_helpers._mod_settings(self, group)

    def _bind_log_group(self, group: str) -> None:
        """面板请求没有平台事件上下文，但可以从 payload 里拿到 group；绑定到日志上下文。

        contextvars 按协程/任务隔离，请求结束自动失效，不需要手动 reset。
        """
        try:
            if group:
                LOG_CONTEXT.set(self._ctx("", str(group)))
        except Exception:
            # noqa: silent-cleanup —— 降级兜底，不影响主流程
            pass

    @staticmethod
    def _lb(value, n: int = 6) -> str:
        """短标识：日志里统一取后 6 位，完整值只在 DEBUG 出现。"""
        return short_id(value, n)

    def _ctx(self, platform: str = "", group: str = "", member: str = "", record=None) -> str:
        """日志上下文前缀：[实例 xxx|群 yyy|成员 zzz|记录 #id]。

        统一格式，避免「这条日志是哪个机器人/哪个群」靠猜。
        """
        parts = []
        if platform:
            parts.append(f"实例 {platform}")
        if group:
            parts.append(f"群 {self._lb(group)}")
        if member:
            parts.append(f"成员 {self._lb(member)}")
        if record is not None:
            parts.append(f"记录 #{record}")
        return ("[" + "|".join(parts) + "] ") if parts else ""

    # 判定记录里留存的原文上限（字）。旧版只留 200 字，复核时看不到全貌。
    _VERDICT_TEXT_CAP = 1000

    def _verdict_text(self, rep: dict) -> dict:
        """记录原文归档；实现见 qgm.features.audit.record。"""
        return audit_record.verdict_text(rep, int(self._VERDICT_TEXT_CAP))

    # moderation 话术模板里的内置模块占位符：{at} / {avatar} / {avatar:N}
    _MOD_MODULE_RE = re.compile(r"\{at\}|\{avatar(?::\d+)?\}")

    def _mod_tpl(self, group: str, key: str, **kw) -> str:
        """话术渲染：本群覆盖 -> 面板全局话术 -> 插件配置 -> 内置默认。

        注意 moderation.render_tpl 会把「未提供的 {变量}」一并清空，
        所以内置模块占位符（{at} / {avatar}）必须先换成哨兵保护起来，
        渲染完再还原，否则会像写错变量一样被静默吃掉。
        """
        # 1.9.0 迁移：审核话术统一走「设置 → 消息模板」（插件配置 templates.<key>）。
        # 群规审核里的旧「本群覆盖 / 全局覆盖」保留数据但不再参与渲染，给一个版本迁移期。
        tpl = ""
        if hasattr(self, "config"):
            tpl = self._read_config_value(
                ("templates", key), default="", legacy_keys=(key,)
            )
        if not str(tpl or "").strip():
            tpl = moderation.DEFAULT_TEMPLATES.get(key, "")

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

    def _find_recent_msg(self, group: str, text: str='', ts: float=0.0, max_age: float=3600.0):
        """薄壳：见 qgm/features/audit/media.py。"""
        return audit_media._find_recent_msg(self, group, text, ts, max_age)

    def _mod_context_lines(self, group: str, rep: dict, settings: dict):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return audit_helpers._mod_context_lines(self, group, rep, settings)

    def _nearby_images(self, group: str, sender: str, anchor_ts: float, window: float=3.0, limit: int=4):
        """薄壳：见 qgm/features/audit/media.py。"""
        return audit_media._nearby_images(self, group, sender, anchor_ts, window, limit)

    def _latest_image_msg(self, group: str, max_age: float=120.0):
        """薄壳：见 qgm/features/audit/media.py。"""
        return audit_media._latest_image_msg(self, group, max_age)

    def _quote_attachments_from_raw(self, raw):
        """薄壳：见 qgm/features/audit/media.py。"""
        return audit_media._quote_attachments_from_raw(self, raw)

    def _mod_resolve_reported(self, event: AstrMessageEvent):
        """薄壳：见 qgm/features/audit/quotes.py。"""
        return audit_quotes._mod_resolve_reported(self, event)

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

    async def _mod_provider_info(self, event: Optional[AstrMessageEvent]=None, vision: bool=False, group: str=''):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return await audit_helpers._mod_provider_info(self, event, vision, group)

    async def _mod_provider_id(self, event: Optional[AstrMessageEvent]=None, vision: bool=False, group: str=''):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return await audit_helpers._mod_provider_id(self, event, vision, group)

    # ---------------- 四维度判定 ----------------
    # ------------------------------------------------------------------
    # 变体检测（谐音 / 形近 / 插符号绕过）
    # ------------------------------------------------------------------
    def _variant_sync(self, force: bool = False) -> None:
        """把词库字面量 + 代码内置正则里的字面片段喂给变体索引。

        索引按签名复用：词库没变时只做一次签名比对，不会重复建表。
        """
        sig = ""
        try:
            sig = self.lexicons.signature()
        except Exception:
            sig = ""
        if force or self._variant_words is None or getattr(self, "_variant_sig", "") != sig:
            self._variant_sig = sig
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
            self.variant.set_words(self._variant_words)   # 只在真正重建时同步，避免每次扫描都重建索引

    @staticmethod
    def _mod_extra_variant_words(settings: dict):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return audit_helpers._mod_extra_variant_words(settings)

    @staticmethod
    def _variant_method_name(method: str) -> str:
        return {"pinyin": "谐音改写", "confusable": "形近改写", "abbr": "拼音缩写"}.get(str(method), "变体写法")

    def _mod_variant_scan(self, group: str, rep: dict, settings: dict):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return audit_helpers._mod_variant_scan(self, group, rep, settings)

    async def _mod_variant_action(self, event: AstrMessageEvent, group: str, rep: dict, settings: dict, variant: dict, result: dict):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return await audit_helpers._mod_variant_action(self, event, group, rep, settings, variant, result)

    async def _mod_variant_strike(self, event: AstrMessageEvent, group: str, rep: dict, settings: dict, variant: dict, result: dict, rec: Optional[dict]):
        """薄壳：见 qgm.features.audit.engine。"""
        return await audit_engine._mod_variant_strike(self, event, group, rep, settings, variant, result, rec)

    def _mod_keyword_hit(self, text: str, settings: dict):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return audit_helpers._mod_keyword_hit(self, text, settings)

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
                         scene: str = "举报"):
        """薄壳：判定层见 qgm.features.audit.review。"""
        return audit_review.review_fast(self, group, rep, respect_switches, scene)

    async def _mod_review_llm(self, event, group: str, rep: dict,
                              context_lines: list[str], reporter_note: str = "",
                              pid_override: str = "", respect_switches: bool = True,
                              scene: str = "举报"):
        """薄壳：判定层见 qgm.features.audit.review。"""
        return await audit_review.review_llm(
            self, event, group, rep, context_lines, reporter_note,
            pid_override, respect_switches, scene)

    # ---------------- 执行（撤回 + 禁言） ----------------
    def _mod_plan(self, group: str, verdict: dict, rep: dict, *,
                  settings: dict | None = None, prior_override=None) -> dict:
        """把「判定结果」变成「处罚计划」；纯逻辑在 qgm.features.audit.policy。"""
        st = settings if settings is not None else self._mod_settings(group)
        smart = bool(st.get("smart_penalty", False))
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
        return audit_policy.build_plan(
            verdict=verdict,
            settings=st,
            prior=prior,
            rules=self.mod.get_rules(group),
            default_seconds=int(self._read_config_value(
                ("moderation", "default_mute_seconds"), default=600) or 600),
            decide_action=moderation.decide_action,
            escalate_seconds=moderation.escalate_seconds,
            fmt_duration=fmt_duration,
            warn=self.logger.warning,
        )

    _LEGACY_TEMPLATE_DEFAULTS = {
        "msg_mod_revoke": (
            "✅ 已撤销处罚记录 #{vid}\n"
            "{at} 经复核，本条判定有误，已撤销对该成员的处罚并致歉。\n"
            "原处罚：{punish}\n禁言状态：{unmute}\n撤销人：{operator}{reason_line}"
        ),
    }

    def _migrate_template_defaults(self) -> None:
        """旧版默认文案（逐字完全相同才替换）自动升级；用户改过的不动。"""
        try:
            tpls = dict(self.config.get("templates") or {})
        except Exception:
            return
        changed = False
        for key, old_text in self._LEGACY_TEMPLATE_DEFAULTS.items():
            if str(tpls.get(key) or "") == old_text:
                tpls[key] = moderation.DEFAULT_TEMPLATES.get(key) or old_text
                changed = True
        if not changed:
            return
        try:
            self.config["templates"] = tpls
            self.config.save_config()
            self.logger.info("[模板] 已升级撤销致歉默认文案（含独立致歉句）")
        except Exception as e:
            self.logger.debug(f"[模板] 默认文案升级未写盘（仅本次生效）：{e}")

    @staticmethod
    def _plan_trace(plan: dict, st: dict, conf: float) -> dict:
        """写进判定记录的决策依据；实现见 qgm.features.audit.policy。"""
        return audit_policy.plan_trace(plan, st, conf)

    def _mod_predict_action(self, group: str, verdict: dict, rep: dict, prior_override: int | None=None):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return audit_helpers._mod_predict_action(self, group, verdict, rep, prior_override)

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
                # noqa: silent-cleanup —— 降级兜底，不影响主流程
                pass
            return False, "动作队列繁忙（并发上限 2），本次未执行"
        try:
            await coro
            return True, ""
        finally:
            self._mod_action_sem.release()

    async def _mod_try_recall(self, event: AstrMessageEvent, group: str, rep: dict, settings: dict):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return await audit_helpers._mod_try_recall(self, event, group, rep, settings)

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

    async def _mod_execute(self, event: AstrMessageEvent, group: str, rep: dict, verdict: dict, settings: dict):
        """薄壳：见 qgm.features.audit.engine。"""
        return await audit_engine._mod_execute(self, event, group, rep, verdict, settings)

    def _is_qq_admin_openid(self, group: str, member: str) -> bool:
        try:
            return member in (getattr(self, "_qq_group_admins", None) or {}).get(group, set())
        except Exception:
            return False

    def _mod_group_allowed(self, group: str):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return audit_helpers._mod_group_allowed(self, group)

    async def _mod_auto_variant_only(self, event: AstrMessageEvent, group: str, rep: dict, settings: dict, variant: dict):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return await audit_helpers._mod_auto_variant_only(self, event, group, rep, settings, variant)

    async def _maybe_auto_scan(self, event):
        """薄壳：见 audit_scan。"""
        return await audit_scan._maybe_auto_scan(self, event)

    # ---------------- /举报 ----------------
    @_with_log_context
    async def _cmd_report_violation(self, event: AstrMessageEvent, parts: list[str]) -> None:
        """薄壳：举报审核编排见 qgm.features.audit.service。"""
        return await audit_service.report_violation(self, event, parts)

    # ---------------- /群规 ----------------
    async def _cmd_admin_register(self, event: AstrMessageEvent, parts: list[str] | None=None):
        """薄壳：见 admin_commands。"""
        return await admin_commands._cmd_admin_register(self, event, parts)

    async def _cmd_rules(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 audit_commands。"""
        return await audit_commands._cmd_rules(self, event, parts)

    # ---------------- /审核（管理员） ----------------
    async def _cmd_mod(self, event: AstrMessageEvent, parts: list[str]):
        """薄壳：见 audit_commands。"""
        return await audit_commands._cmd_mod(self, event, parts)

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

    def _mod_group_entry(self, group: str):
        """薄壳：见 qgm/features/audit/helpers.py。"""
        return audit_helpers._mod_group_entry(self, group)

    async def _refresh_bot_name(self, platform_id: str, ttl: float=120.0):
        """薄壳：见 qgm/features/members/cache.py。"""
        return await members_cache._refresh_bot_name(self, platform_id, ttl)

    async def web_mod_groups(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_groups(self)

    async def web_mod_group_rename(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_group_rename(self)

    async def web_mod_group_names_refresh(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_group_names_refresh(self)

    def _all_known_group_list(self) -> list[str]:
        groups = set(self._known_groups.keys())
        groups |= set((self._nick_cache or {}).keys())
        groups |= set((self._recent_group_msgs or {}).keys())
        groups |= {x["group"] for x in self.mod.groups_overview()}
        return sorted(x for x in groups if x)

    async def web_mod_detail(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_detail(self)

    async def web_mod_rules_save(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_rules_save(self)

    async def web_mod_rules_transfer(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_rules_transfer(self)

    async def web_mod_settings_save(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_settings_save(self)

    async def web_mod_templates_save(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_templates_save(self)

    async def web_mod_verdicts_revoke(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_verdicts_revoke(self)

    async def web_mod_verdicts_prune(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_verdicts_prune(self)

    async def web_mod_verdicts_export(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_verdicts_export(self)

    async def web_mod_verdicts(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_verdicts(self)

    async def web_mod_feedback(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_feedback(self)

    # ------------------------------------------------------------------
    # 成员档案 Web API
    # ------------------------------------------------------------------
    async def web_members(self):
        """薄壳：members_web。"""
        return await members_web.web_members(self)

    async def web_members_stats(self):
        """薄壳：members_web。"""
        return await members_web.web_members_stats(self)

    async def web_members_update(self):
        """薄壳：members_web。"""
        return await members_web.web_members_update(self)

    async def web_members_delete(self):
        """薄壳：members_web。"""
        return await members_web.web_members_delete(self)

    async def web_members_clear_left(self):
        """薄壳：members_web。"""
        return await members_web.web_members_clear_left(self)

    async def web_members_import(self):
        """薄壳：members_web。"""
        return await members_web.web_members_import(self)

    async def web_members_sync(self):
        """薄壳：members_web。"""
        return await members_web.web_members_sync(self)

    async def web_mod_admins(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_admins(self)

    async def web_mod_admins_remove(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_admins_remove(self)

    async def web_mod_todo(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_todo(self)

    async def web_mod_todo_handle(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_todo_handle(self)

    async def web_mod_stats(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_stats(self)

    async def web_mod_test(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_test(self)

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
            # noqa: silent-cleanup —— 降级兜底，不影响主流程
            pass
        except Exception:                                          # noqa: BLE001
            # noqa: silent-cleanup —— 降级兜底，不影响主流程
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

    def _fetch_upstream_lexicon(self, url='', name='', category='', auto_enable=None, mirror='', force_mirror=None):
        """薄壳：见 qgm/features/lexicon/upstream.py。"""
        return lexicon_upstream.fetch_upstream_lexicon(self, url, name, category, auto_enable, mirror, force_mirror)

    async def web_lexicons(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicons(self)

    async def web_lexicon_detail(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_detail(self)

    async def web_lexicon_toggle(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_toggle(self)

    async def web_lexicon_apply(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_apply(self)

    async def web_lexicon_upload(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_upload(self)

    async def web_lexicon_fetch_upstream(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_fetch_upstream(self)

    async def web_lexicon_delete(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_delete(self)

    async def web_lexicon_rename(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_rename(self)

    async def web_lexicon_export(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_export(self)

    async def web_lexicon_allow(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_allow(self)

    async def web_lexicon_test(self):
        """薄壳：词库 API 见 qgm.features.lexicon.web。"""
        return await lexicon_web.web_lexicon_test(self)

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
        """薄壳：见 approval_web。"""
        return await approval_web.web_join_approval_list(self)

    async def web_join_approval_save(self):
        """薄壳：见 approval_web。"""
        return await approval_web.web_join_approval_save(self)

    async def web_join_approval_toggle(self):
        """薄壳：见 approval_web。"""
        return await approval_web.web_join_approval_toggle(self)

    async def web_join_approval_delete(self):
        """薄壳：见 approval_web。"""
        return await approval_web.web_join_approval_delete(self)

    async def web_join_approval_execute(self):
        """薄壳：见 approval_web。"""
        return await approval_web.web_join_approval_execute(self)

    async def web_join_approval_whitelist(self):
        """薄壳：见 approval_web。"""
        return await approval_web.web_join_approval_whitelist(self)

    def _register_web_apis(self) -> None:
        """薄壳：见 qgm/web/registry.py。"""
        return web_registry._register_web_apis(self)

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
        """薄壳：见 status_web。"""
        return await status_web.web_status(self)

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
        """薄壳：见 mute_web。"""
        return await mute_web.web_mutes(self)

    async def web_mute_history(self):
        """薄壳：见 mute_web。"""
        return await mute_web.web_mute_history(self)

    async def web_mute_history_clear(self):
        """薄壳：见 mute_web。"""
        return await mute_web.web_mute_history_clear(self)

    async def web_mute_history_delete(self):
        """薄壳：见 mute_web。"""
        return await mute_web.web_mute_history_delete(self)

    async def web_recalls(self):
        """薄壳：见 mute_web。"""
        return await mute_web.web_recalls(self)

    async def web_recalls_clear(self):
        """薄壳：见 mute_web。"""
        return await mute_web.web_recalls_clear(self)

    async def web_recalls_delete(self):
        """薄壳：见 mute_web。"""
        return await mute_web.web_recalls_delete(self)

    async def web_requests(self):
        """薄壳：见 status_web。"""
        return await status_web.web_requests(self)

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
        """薄壳：status_web。"""
        return await status_web.web_logs(self)

    async def web_logs_stream(self):
        """薄壳：status_web。"""
        return await status_web.web_logs_stream(self)

    async def web_logs_clear(self):
        """薄壳：status_web。"""
        return await status_web.web_logs_clear(self)

    async def web_poll_now(self):
        """薄壳：见 status_web。"""
        return await status_web.web_poll_now(self)

    async def web_config_save(self):
        """薄壳：配置 API 见 qgm.features.config.web。"""
        return await config_web.web_config_save(self)

    async def web_mod_verdicts_delete(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_verdicts_delete(self)

    async def web_mod_verdicts_clear(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_mod_verdicts_clear(self)

    # WebUI 设置页：配置 schema / 当前值 / 保存（与 AstrBot 配置双向同步）
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
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_tpl_formats(self)

    async def web_tpl_formats_save(self):
        """薄壳：审核域 API 见 qgm.features.audit.web。"""
        return await audit_web.web_tpl_formats_save(self)

    async def web_config_schema(self):
        """薄壳：配置 API 见 qgm.features.config.web。"""
        return await config_web.web_config_schema(self)

    async def web_config_bulk_save(self):
        """薄壳：配置 API 见 qgm.features.config.web。"""
        return await config_web.web_config_bulk_save(self)
