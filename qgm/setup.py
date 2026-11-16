"""插件初始化装配（原 __init__ 主体）。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from astrbot.api.star import Star
from ..qq_api import QQApi
from ..plugin_logger import PluginLogger
from .. import lexicon as lexicon_lib, member_registry as member_registry_lib, moderation, variant as variant_lib
from .kernel.constants import PLUGIN_NAME


def init_plugin(host, context, config=None):
        Star.__init__(host, context)
        host.config = config
        host.logger = PluginLogger(
            max_entries=int(host._read_config_value(("log", "log_max_entries"), default=300, legacy_keys=("log_max_entries",))),
            level=str(host._read_config_value(("log", "log_level"), default="DEBUG", legacy_keys=("log_level",))),
            to_file=bool(host._read_config_value(("log", "log_to_file"), default=True, legacy_keys=("log_to_file",))),
            keep_days=int(host._read_config_value(("log", "log_keep_days"), default=14, legacy_keys=("log_keep_days",))),
            log_dir=host._plugin_data_dir(),
            plugin_name=PLUGIN_NAME,
        )
        try:
            host.logger.concise = bool(host._read_config_value(
                ("log", "log_concise"), default=False, legacy_keys=("log_concise",)))
        except Exception:
            host.logger.concise = False
        host._check_plugin_set()
        host._schema_cache: dict | None = None  # _conf_schema.json 缓存（设置页用）
        host._apis: dict[str, QQApi] = {}
        host._known_groups: dict[str, str] = {}  # group_openid -> platform_id
        host._intro_done: dict[str, str] = {}    # 已发过群管自我介绍的群 -> event/message/manual
        host._intro_pending: dict[str, float] = {}  # 收到入群事件、但发送尚未确认成功的群
        host._nick_cache: dict[str, dict[str, str]] = {}  # group_openid -> {member_openid: 昵称}
        # 被 40034105（该群未开主动消息权限）拒过的群，仅用于日志/诊断提示，不阻止重试
        host._member_perm_blocked: set[str] = set()
        host._pending: dict[str, list[dict]] = {}  # group_openid -> [join_request]
        host._notified: dict[str, dict[tuple, float]] = {}  # group_openid -> {申请键: 通知时间}（防重复推送）
        # 轮询任务自己维护"上次看到的申请键"，不依赖共享的 host._pending：
        # _pending 会被 /审批 指令、WebUI /requests 接口更新，若拿它当"上一轮"
        # 对比，申请会被提前"见过"，导致新申请检测永远为空、通知永远不发送。
        host._seen_requests: dict[str, set] = {}  # group_openid -> {申请键}
        # 机器人不是群管理员（11703）时轮询入群申请必然失败：按群退避，
        # 避免每 30 秒刷一条 ERROR 把日志刷到几 MB。
        host._join_error_code: dict[str, int | None] = {}   # group_openid -> 最近一次错误码
        host._join_backoff: dict[str, float] = {}           # group_openid -> 下次允许轮询的时间戳
        host._join_denied_logged: set[str] = set()          # 已提示过 11703 的群
        # ---- 1.4.0 入群申请事件化 + 消息按钮 ----
        # GROUP_JOIN_REQUEST 事件到达时间（按群）：用来判断「事件通道是否真的在工作」，
        # 以及 event_first 模式下轮询要不要兜底。
        host._join_event_at: dict[str, float] = {}
        # 事件总数 / 最近一次全局时间（面板「概览」展示用）
        host._join_event_total = 0
        host._join_event_last = 0.0
        # 互动事件（按钮点击）去重：同一个 interaction 只处理一次。
        # 官方「同一 interaction_id 只能回应一次」，但用户可能连点，这里做幂等。
        host._interaction_done: dict[str, float] = {}
        # 互动事件统计（面板展示）
        host._interaction_total = 0
        host._interaction_last = 0.0
        host._interaction_last_result = ""
        # 申请事件合并队列：同一群 2 秒内到达的多个申请合并成一条通知，
        # 避免「三个人同时申请 → 群里刷三条消息」。
        host._join_fresh: dict[str, list[dict]] = {}
        host._join_notify_tasks: dict[str, asyncio.Task] = {}
        host._member_event_ok = False  # 群成员事件注入是否已成功
        host._last_inject_attempt = 0.0  # 最近一次注入尝试时间戳（防频繁重连）
        host._mute_history: dict[str, list[dict]] = {}  # group_openid -> [记录]
        host._history_seq = 0
        host._recall_history: dict[str, list[dict]] = {}  # group_openid -> [撤回记录]
        host._recall_seq = 0
        # 消息模板的发送格式（text / markdown / image）与图片地址。
        # 注意：模板"内容"仍保存在 AstrBot 配置里（与原生面板兼容、老配置零迁移），
        # 这里只记录"这段内容该怎么发"，属于纯展示层元数据，丢了也只是退化成纯文本。
        host._tpl_formats: dict[str, dict] = host._load_tpl_formats()
        # 入群欢迎去重：{"群openid:成员openid": 时间戳}。
        # 同一个入群事件可能被重推（断线重连补事件）；审批通过路径已经发过
        # 欢迎语时，紧随其后的 GROUP_MEMBER_ADD 也不该再发一遍。
        host._join_welcomed: dict[str, float] = {}
        # 群规审核（/举报）：数据按群隔离、全部本地落盘；
        # 注意：只有收到 /举报 才会调用模型，平时不扫描、不判定、不执行。
        host.mod = moderation.ModerationStore(host._plugin_data_dir(), host.logger)
        host._migrate_template_defaults()
        try:
            host.mod.verdict_max_records = int(
                host._read_config_value(("moderation", "verdict_max_records"), default=5000,
                                        legacy_keys=("verdict_max_records",)) or 0
            )
        except Exception:
            host.mod.verdict_max_records = 5000
        # 面板/配置页改了上限后不必重载：每次追加时读一次（内存字典读取，成本可忽略）
        host.mod.verdict_limit_provider = lambda: int(
            host._read_config_value(("moderation", "verdict_max_records"), default=5000,
                                    legacy_keys=("verdict_max_records",)) or 0
        )
        # 群成员档案（openid -> 昵称/备注）：官方事件与消息里只有 openid、没有昵称，
        # 所以"这个人是谁"必须落一张可人工维护的表；退群通知、禁言列表都从这取名。
        host.members = member_registry_lib.MemberRegistry(
            host._plugin_data_dir(), host.logger
        )
        # 关键词词库：内置的是公开开源词库（lexicons/SOURCES.md 有来源与协议），
        # 页面可上传自己的词库、勾选启用、加白名单；词库只做关键词维度，不单独调模型。
        host.lexicons = lexicon_lib.LexiconManager(
            host._plugin_data_dir(), plugin_dir=os.path.dirname(os.path.abspath(__file__)), logger=host.logger
        )
        host.logger.info(
            "[词库] 内置词库仅 2 个（广告引流 / 辱骂攻击，来自公开开源词表，见 lexicons/SOURCES.md），"
            "只用于本机内容过滤与拦截，不对外发布、不代表作者立场；词条内容不在面板展示。"
            "其他类型词表请自行上传或运行 tools/build_lexicons.py 生成"
        )
        # 变体检测（谐音/形近/插符号绕过）：只产出「疑似」信号，命中只撤回+记录，永不自动禁言。
        # 拼音表与形近表都是离线数据文件，缺失时自动降级（不抛异常）。
        host.variant = variant_lib.VariantMatcher(
            os.path.dirname(os.path.abspath(__file__)), host.logger
        )
        host._variant_words: Optional[list[tuple[str, tuple]]] = None  # 词条缓存（签名比对在建表侧做）
        host._mod_pending: set[str] = set()  # 正在审核的消息 ID，防重复受理
        # 模型调用闸门：同群并发 1，排队数超过 llm_queue_limit 的消息直接跳过模型层，
        # 避免刷屏群把 provider 打满、把每日额度一瞬间烧完。
        host._mod_llm_sem: dict[str, asyncio.Semaphore] = {}
        host._mod_llm_pending: dict[str, int] = {}
        host._mod_last_llm: dict = {}  # 最近一次模型调用摘要（/群管 自检 用）
        host._mod_few_shot_base: str = "".join(
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
        host._recent_group_msgs: dict[str, list[dict]] = {}
        # 最近消息缓存落盘：重载/重启后仍能定位「重载前发送的消息」，否则举报只能判不能罚
        host._recent_cache_path = os.path.join(host._plugin_data_dir(), "recent_msgs.json")
        host._recent_saved_at = 0.0
        host._load_recent_msgs()
        host._poll_task: Optional[asyncio.Task] = None
        # 定时汇报（日报）后台任务与"今天已经发过"的日期标记
        host._report_task: Optional[asyncio.Task] = None
        host._report_last_day: str = ""
        # 最近一次有活动的群（日报默认发到这里；在 _record_recent_msg 里更新）
        host._last_active_group: str = ""
        host._initialized = False
        # 防重入：同一消息可能被多个正则 handler 命中（如 /禁言 与 /禁言历史），
        # 用消息 ID 在短时间内去重，避免重复执行导致重复回复。
        host._last_cmd_msg_id: str = ""
        host._last_cmd_ts: float = 0.0

        host._load_known_groups()
        host._load_intro_state()
        host._load_mute_history()
        host._load_recall_history()
        host._load_report_state()
        host._register_web_apis()
        host._rebuild_mod_few_shot()
        host.logger.info(
            f"QQ官机群管 已加载 (log_level={host._read_config_value(('log', 'log_level'), default='DEBUG', legacy_keys=('log_level',))}, "
            f"poll={host._read_config_value(('approval', 'poll_interval'), default=30, legacy_keys=('poll_interval',))}s)"
        )
