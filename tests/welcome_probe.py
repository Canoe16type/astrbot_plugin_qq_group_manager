"""欢迎链路行为探针：同一套场景跑一遍，输出规范化 trace（用于新旧对比）。

用法：python welcome_probe.py <插件父目录>
"""
import asyncio, importlib, json, os, sys, types

root = os.path.abspath(sys.argv[1])
sys.path.insert(0, root)
# 包名按目录名推导：ZIP 下载的目录可能带 -main 后缀
m = importlib.import_module(os.path.basename(root.rstrip("/")) + ".main")
Plugin = next(o for o in vars(m).values() if isinstance(o, type) and hasattr(o, "_maybe_welcome_new_group"))

TPL_DEFAULT = {
    "msg_welcome_after_approve": "欢迎 {username}",
    "msg_group_intro": "我是 {bot_name}，本群功能说明……",
    "msg_welcome_new_group": "大家好，我是 {bot_name}（兜底旧文案）",
}


class Log:
    def __getattr__(self, k):
        return lambda *a, **kw: None


class FakeMod:
    def __init__(self):
        self.meta = {}
    def get_group_meta(self, g):
        return self.meta.setdefault(g, {"name": "测试群"})
    def set_group_meta(self, g, **kw):
        self.meta.setdefault(g, {}).update(kw)


class FakeApi:
    def __init__(self, sent, fail=False):
        self.sent = sent
        self.fail = fail
    async def fetch_group_info(self, g):
        return ({"group_name": "测试群"}, None)
    async def send_group_message(self, g, text, event_id=""):
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(text)


class FakeEvent:
    def __init__(self, gid, sent):
        self._g = gid
        self.sent = sent
        self.message_obj = types.SimpleNamespace(message_id="MID")
    def get_group_id(self):
        return self._g
    def get_platform_id(self):
        return "bot1"
    async def send(self, chain):
        self.sent.append(str(chain))


def make(cfg, sent, send_fail=False, initialized=True, known=None, done=None, pending=None, templates=None):
    p = object.__new__(Plugin)
    p.logger = Log()
    p.mod = FakeMod()
    p._known_groups = dict(known or {})
    p._initialized = initialized
    p._intro_done = dict(done or {})
    p._intro_pending = dict(pending or {})
    p._poll_task = None
    p._save_known_groups = lambda: None
    p._save_intro_state = lambda: None
    tpl = dict(templates if templates is not None else TPL_DEFAULT)
    def rcv(path, default=None, legacy_keys=None):
        if path[0] == "basic":
            return cfg.get(path[1], default)
        if path[0] == "templates":
            return tpl.get(path[1], "")
        return default
    p._read_config_value = rcv
    p._render = lambda key, **kw: (tpl.get(key) or "").format(**{k: str(v) for k, v in kw.items()})
    api = FakeApi(sent, fail=send_fail)
    p._get_api = lambda pid: api
    p._api_for_group_or_default = lambda *a, **k: api
    async def rp(ev, text):
        if text:
            sent.append(text)
    p._reply = rp
    return p


class Data:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def run_all():
    out = {}

    async def case(name, cfg, *, event_ok=True, send_fail=False, do_event=True, msg=True, **kw):
        sent = []
        p = make(cfg, sent, send_fail=send_fail, **kw)
        ev = FakeEvent("G1", sent)
        if do_event:
            await p._on_group_add_robot(Data(group_openid="G1", event_id="EV1", op_member_openid="U1"), "bot1")
        if msg:
            await p._maybe_welcome_new_group(ev)
        out[name] = {
            "sent": sent,
            "intro_done": dict(p._intro_done),
            "pending": sorted(p._intro_pending.keys()),
        }

    FULL = {"enable_welcome": True, "enable_group_add_intro": True, "enable_new_group_welcome": True}

    # S1 事件成功 → 首条消息不发
    await case("S1_事件成功_首条消息", FULL, do_event=True, msg=False)
    await case("S1b_事件成功后再来首条消息", FULL, do_event=True, msg=True, known={"G1": "bot1"})
    # S2 事件发送失败 → 首条消息兜底（旧文案）
    await case("S2_事件失败_首条消息", FULL, send_fail=True, known={"G1": "bot1"})
    # S3 自我介绍关 + 兜底开 → 事件不发，首条消息补发
    await case("S3_自我介绍关_兜底开", {**FULL, "enable_group_add_intro": False}, known={"G1": "bot1"})
    # S4 兜底关 + 事件失败 → 首条消息不发
    await case("S4_兜底关_事件失败", {**FULL, "enable_new_group_welcome": False}, send_fail=True, known={"G1": "bot1"})
    # S5 新发现的群 + 插件未初始化 → 只登记不发言
    await case("S5_新群未初始化", FULL, do_event=False, initialized=False)
    # S6 新发现的群 + 插件已初始化 → 走兜底
    await case("S6_新群已初始化", FULL, do_event=False, initialized=True)
    # S7 兜底话术留空（新默认值）→ 新代码复用自我介绍 / 旧代码发空
    await case("S7_兜底话术留空", FULL, send_fail=True, known={"G1": "bot1"},
               templates={**TPL_DEFAULT, "msg_welcome_new_group": ""})
    # S8 用户自定义兜底话术 → 两边都发自定义
    await case("S8_自定义兜底话术", FULL, send_fail=True, known={"G1": "bot1"},
               templates={**TPL_DEFAULT, "msg_welcome_new_group": "自定义：{bot_name} 就位"})
    # S9 自我介绍文案为空 → 事件路径视为失败 → 首条消息兜底
    await case("S9_自我介绍为空", FULL, known={"G1": "bot1"},
               templates={**TPL_DEFAULT, "msg_group_intro": ""})
    # S10 已发过自我介绍（done=event）再来首条消息 → 不发
    await case("S10_已发过", FULL, do_event=False, known={"G1": "bot1"}, done={"G1": "event"})

    print(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True))


asyncio.run(run_all())
