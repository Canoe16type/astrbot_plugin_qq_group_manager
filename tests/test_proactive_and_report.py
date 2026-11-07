"""回归测试：主动消息的按钮/格式约束 + 定时汇报 + 审批列表分页。

覆盖三个线上反馈：
1. **按钮不弹**：模板格式设成纯文本时，keyboard 被平台静默忽略，通知里
   只剩文字（用户看到「发了申请没按钮」）。修复后带按钮必须走 markdown 通道。
2. **定时汇报**：manage 在 WebUI 设 HH:MM，到点主动推一条日报；
   同一天只发一条；AstrBot 错过时刻后启动能补发。
3. **完整申请列表**：/群管 审批 支持分页与「全部」跨群汇总。

跑法（插件目录下）：
    python3 -m unittest discover -s tests -v
"""

import importlib
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

QQGroupManager = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.main"
).QQGroupManager
QQApiError = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.qq_api"
).QQApiError
button_kit = importlib.import_module(
    "plugins.astrbot_plugin_qq_group_manager.button_kit"
)


class _Logger:
    def __init__(self):
        self.lines = []

    def _add(self, lvl, msg):
        self.lines.append((lvl, str(msg)))

    def debug(self, m):
        self._add("debug", m)

    def info(self, m):
        self._add("info", m)

    def warning(self, m):
        self._add("warning", m)

    def error(self, m):
        self._add("error", m)


class _FakeApi:
    """记录每次发送用的参数，可按需模拟失败档位。

    - fail_markdown：所有 markdown 调用都失败（模拟 markdown 权限/参数问题）
    - fail_gid：markdown 里带 group_id 就失败（复现线上 40011021）
    - fail_all_code：任何调用都失败（模拟无权限/频控这类换格式没用的错）
    """

    def __init__(self, fail_markdown=False, fail_code=40034124, fail_gid=False,
                 fail_all_code=None):
        self.calls = []
        self.fail_markdown = fail_markdown
        self.fail_code = fail_code
        self.fail_gid = fail_gid
        self.fail_all_code = fail_all_code

    async def send_group_message(self, group_openid, content, **kw):
        self.calls.append((content, dict(kw)))
        if self.fail_all_code:
            raise QQApiError("发送失败", code=self.fail_all_code)
        if kw.get("markdown") and self.fail_markdown:
            raise QQApiError("markdown 参数错误", code=self.fail_code)
        if (kw.get("markdown") and self.fail_gid
                and button_kit.has_group_id(kw.get("keyboard"))):
            raise QQApiError("请求参数rows.buttons.group_id无效", code=40011021)

    async def get_mute_state(self, group_openid):
        return {"members": [{"member_openid": "u1"}]}


def _tpl_text(text, key="msg_join_request_notify"):
    """模拟 _render 返回的 _TplText（带 tpl_key 与 tpl_target）。"""
    class _T(str):
        pass

    t = _T(text)
    t.tpl_key = key
    t.tpl_target = ""
    return t


def _proactive_mgr(fmt="text", api=None):
    m = types.SimpleNamespace()
    m.logger = _Logger()
    m._tpl_formats = {"msg_join_request_notify": {"format": fmt}}
    m._tpl_format = lambda key: fmt
    m._expand_msg_modules = lambda text, ev, fmt_, target, **kw: text
    m._api_for_group_or_default = lambda g, pid: api
    m.KEYBOARD_DROP_CODES = QQGroupManager.KEYBOARD_DROP_CODES
    m.FATAL_SEND_CODES = QQGroupManager.FATAL_SEND_CODES
    m._explain_send_error = lambda code: f"code={code}"
    return m


KEYBOARD = {"content": {"rows": [{"buttons": [{"id": "1"}]}]}}
KEYBOARD_GID = {
    "content": {"rows": [{"buttons": [
        {"id": "1", "group_id": button_kit.make_group_id("u1", "2026-11-07T14:01:25+08:00")},
    ]}]}
}


class ProactiveButtonFormatTest(unittest.IsolatedAsyncioTestCase):
    """模板是纯文本、但带了按钮 → 必须改用 markdown 发送。"""

    async def test_text_template_with_keyboard_uses_markdown(self):
        api = _FakeApi()
        m = _proactive_mgr(fmt="text", api=api)
        ok = await QQGroupManager._send_group_proactive(
            m, "g1", _tpl_text("申请通知"), "pid", keyboard=KEYBOARD
        )
        self.assertTrue(ok)
        self.assertEqual(len(api.calls), 1)
        _, kw = api.calls[0]
        self.assertTrue(kw.get("markdown"), "带按钮必须走 markdown，否则按钮不渲染")
        self.assertEqual(kw.get("keyboard"), KEYBOARD)

    async def test_text_template_without_keyboard_stays_text(self):
        api = _FakeApi()
        m = _proactive_mgr(fmt="text", api=api)
        ok = await QQGroupManager._send_group_proactive(m, "g1", _tpl_text("通知"), "pid")
        self.assertTrue(ok)
        _, kw = api.calls[0]
        self.assertFalse(kw.get("markdown"))
        self.assertNotIn("keyboard", kw)

    async def test_group_id_rejected_retries_without_group_id(self):
        """markdown 通道拒 group_id（40011021）→ 去掉 group_id 重发，按钮必须还在。"""
        api = _FakeApi(fail_gid=True)
        m = _proactive_mgr(fmt="markdown", api=api)
        ok = await QQGroupManager._send_group_proactive(
            m, "g1", _tpl_text("通知"), "pid", keyboard=KEYBOARD_GID
        )
        self.assertTrue(ok)
        self.assertEqual(len(api.calls), 2, "第一档失败后只降一档，不该直接丢按钮")
        self.assertTrue(api.calls[1][1].get("markdown"))
        kb = api.calls[1][1].get("keyboard")
        self.assertTrue(kb, "按钮不能丢")
        self.assertFalse(button_kit.has_group_id(kb), "重发时不应再带 group_id")

    async def test_all_markdown_tiers_fail_falls_back_to_text(self):
        """markdown 整条通道都不可用：宁可没按钮没排版，也要把正文发出去。"""
        api = _FakeApi(fail_markdown=True)
        m = _proactive_mgr(fmt="text", api=api)
        ok = await QQGroupManager._send_group_proactive(
            m, "g1", _tpl_text("申请通知"), "pid", keyboard=KEYBOARD
        )
        self.assertTrue(ok)
        self.assertFalse(api.calls[-1][1].get("markdown"))
        self.assertNotIn("keyboard", api.calls[-1][1])

    async def test_permission_error_does_not_retry(self):
        """40034105 无权限：换格式也没用，只发一次，别白刷四次请求。"""
        api = _FakeApi(fail_all_code=40034105)
        m = _proactive_mgr(fmt="text", api=api)
        ok = await QQGroupManager._send_group_proactive(
            m, "g1", _tpl_text("通知"), "pid", keyboard=KEYBOARD_GID
        )
        self.assertFalse(ok)
        self.assertEqual(len(api.calls), 1)


class ReportCandidatesTest(unittest.TestCase):
    """汇报群展开：留空＝所有群；填了＝只汇报匹配到的群（支持多个）。"""

    def _mgr(self, known):
        m = types.SimpleNamespace()
        m.logger = _Logger()
        m._known_groups = dict(known)
        m.mod = types.SimpleNamespace(get_group_meta=lambda g: {}, display_name=lambda g: "")
        m._match_group = types.MethodType(QQGroupManager._match_group, m)
        return m

    def test_empty_target_means_all_groups(self):
        m = self._mgr({"g1": "p1", "g2": "p2"})
        self.assertEqual(
            QQGroupManager._report_candidates(m, ""), [("g1", "p1"), ("g2", "p2")]
        )

    def test_single_openid(self):
        m = self._mgr({"g1": "p1", "g2": "p2"})
        self.assertEqual(QQGroupManager._report_candidates(m, "g2"), [("g2", "p2")])

    def test_multiple_tokens_and_dedupe(self):
        m = self._mgr({"g1": "p1", "g2": "p2", "g3": "p3"})
        out = QQGroupManager._report_candidates(m, "g2, g3 g2")
        self.assertEqual(out, [("g2", "p2"), ("g3", "p3")])

    def test_target_matched_by_group_name(self):
        m = self._mgr({"g1": "p1", "g2": "p2"})
        m.mod = types.SimpleNamespace(
            get_group_meta=lambda g: {"name": "闲聊群" if g == "g2" else "技术群"},
            display_name=lambda g: {"g2": "闲聊群"}.get(g, "技术群"),
        )
        self.assertEqual(QQGroupManager._report_candidates(m, "闲聊群"), [("g2", "p2")])

    def test_unknown_token_skipped(self):
        m = self._mgr({"g1": "p1"})
        self.assertEqual(QQGroupManager._report_candidates(m, "nope"), [])
        self.assertTrue(any(lvl == "warning" for lvl, _ in m.logger.lines))

    def test_no_groups(self):
        m = self._mgr({})
        self.assertEqual(QQGroupManager._report_candidates(m, ""), [])


class MaybeSendReportTest(unittest.IsolatedAsyncioTestCase):
    """到点后按群逐个汇报：只发「有未处理申请」的群，每天一轮。"""

    def _mgr(self, now, cfg=None, pending=None, fail_fetch=()):
        cfg = cfg or {}
        pending = pending if pending is not None else {}
        failed = set(fail_fetch)
        m = types.SimpleNamespace()
        m.logger = _Logger()
        m._report_last_day = ""
        m._save_report_state = lambda: None   # 内存标记即可，测试不落盘
        m._known_groups = dict(cfg.get("known", {"g1": "p1"}))
        m._join_error_code = {}
        m._report_options = lambda: (
            cfg.get("enabled", True), cfg.get("hm", (9, 0)), cfg.get("target", "")
        )
        m._local_now = lambda: now
        m.mod = types.SimpleNamespace(get_group_meta=lambda g: {}, display_name=lambda g: "")
        m._pending_list = lambda gid: list(pending.get(gid, []))
        m._match_group = types.MethodType(QQGroupManager._match_group, m)
        m._report_candidates = types.MethodType(QQGroupManager._report_candidates, m)

        async def _refresh(gid, pid):
            if gid in failed:
                m._join_error_code[gid] = 11703
                return m._pending_list(gid)
            return list(pending.get(gid, []))

        async def _build(g, p, *, pending=None):
            return f"日报:{g}:{len(pending or [])}"

        m._refresh_requests = _refresh
        m._build_report_text = _build

        sent_list = []

        async def _send(g, text, pid, **kw):
            sent_list.append((g, text))
            return g not in set(cfg.get("fail_send", ()))

        m._send_group_proactive = _send
        m._sent = sent_list
        return m

    async def test_only_groups_with_pending_get_report(self):
        m = self._mgr(
            datetime(2026, 11, 7, 9, 3),
            cfg={"known": {"g1": "p1", "g2": "p2", "g3": "p3"}},
            pending={"g1": [{"member_openid": "u1"}], "g3": [{"member_openid": "u2"}]},
        )
        await QQGroupManager._maybe_send_report(m)
        self.assertEqual([g for g, _ in m._sent], ["g1", "g3"], "只发有未处理的群")

    async def test_report_goes_to_its_own_group(self):
        m = self._mgr(
            datetime(2026, 11, 7, 9, 3),
            cfg={"known": {"g1": "p1", "g2": "p2"}},
            pending={"g2": [{"member_openid": "u1"}]},
        )
        await QQGroupManager._maybe_send_report(m)
        self.assertEqual(m._sent, [("g2", "日报:g2:1")], "A 群的申请只发到 A 群")

    async def test_not_before_time(self):
        m = self._mgr(
            datetime(2026, 11, 7, 8, 59), pending={"g1": [{"member_openid": "u1"}]}
        )
        await QQGroupManager._maybe_send_report(m)
        self.assertEqual(m._sent, [])

    async def test_only_once_per_day(self):
        m = self._mgr(
            datetime(2026, 11, 7, 10, 0), pending={"g1": [{"member_openid": "u1"}]}
        )
        await QQGroupManager._maybe_send_report(m)
        await QQGroupManager._maybe_send_report(m)
        self.assertEqual(len(m._sent), 1, "同一天只跑一轮")
        self.assertEqual(m._report_last_day, "2026-11-07")

    async def test_next_day_sends_again(self):
        m = self._mgr(
            datetime(2026, 11, 7, 10, 0), pending={"g1": [{"member_openid": "u1"}]}
        )
        await QQGroupManager._maybe_send_report(m)
        m._local_now = lambda: datetime(2026, 11, 8, 9, 1)
        await QQGroupManager._maybe_send_report(m)
        self.assertEqual(len(m._sent), 2)

    async def test_disabled_does_nothing(self):
        m = self._mgr(
            datetime(2026, 11, 7, 10, 0),
            cfg={"enabled": False},
            pending={"g1": [{"member_openid": "u1"}]},
        )
        await QQGroupManager._maybe_send_report(m)
        self.assertEqual(m._sent, [])

    async def test_fetch_failure_skips_group(self):
        """拉不到申请列表（11703/实例缺失）就别推假日报。"""
        m = self._mgr(
            datetime(2026, 11, 7, 10, 0),
            cfg={"known": {"g1": "p1", "g2": "p2"}},
            pending={"g1": [{"member_openid": "u1"}], "g2": [{"member_openid": "u2"}]},
            fail_fetch={"g2"},
        )
        await QQGroupManager._maybe_send_report(m)
        self.assertEqual([g for g, _ in m._sent], ["g1"])

    async def test_one_group_failure_does_not_block_others(self):
        m = self._mgr(
            datetime(2026, 11, 7, 10, 0),
            cfg={"known": {"g1": "p1", "g2": "p2"}, "fail_send": ("g1",)},
            pending={"g1": [{"member_openid": "u1"}], "g2": [{"member_openid": "u2"}]},
        )
        await QQGroupManager._maybe_send_report(m)
        self.assertEqual([g for g, _ in m._sent], ["g1", "g2"])


class ReportStatePersistenceTest(unittest.IsolatedAsyncioTestCase):
    """日报「今天已发过」必须落盘：重载插件/保存配置后不能重复推一轮。"""

    def _mgr(self, data_dir):
        m = types.SimpleNamespace()
        m.logger = _Logger()
        m._report_last_day = ""
        m._plugin_data_dir = lambda: str(data_dir)
        for name in ("_report_state_path", "_load_report_state", "_save_report_state"):
            setattr(m, name, types.MethodType(getattr(QQGroupManager, name), m))
        return m

    async def test_state_survives_restart(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            m1 = self._mgr(d)
            m1._report_last_day = "2026-11-07"
            m1._save_report_state()

            # 模拟重载插件：新实例内存里没有标记，但应能从磁盘读回来
            m2 = self._mgr(d)
            m2._load_report_state()
            self.assertEqual(m2._report_last_day, "2026-11-07")

    async def test_missing_or_broken_state_does_not_crash(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            m = self._mgr(d)
            m._load_report_state()          # 文件不存在
            self.assertEqual(m._report_last_day, "")
            with open(os.path.join(d, "report_state.json"), "w", encoding="utf-8") as f:
                f.write("{坏的 json")
            m._load_report_state()          # 内容坏了也不抛
            self.assertEqual(m._report_last_day, "")


class RequestListPagingTest(unittest.IsolatedAsyncioTestCase):
    """/群管 审批 [页码] [全部]：分页与跨群汇总。"""

    def _mgr(self, pending):
        m = types.SimpleNamespace()
        m.logger = _Logger()
        m.REQUEST_PAGE_SIZE = 10
        m._pending = pending
        m._pending_list = lambda gid: list(pending.get(gid, []))
        async def _refresh(gid, pid):
            return list(pending.get(gid, []))

        m._refresh_requests = _refresh
        m.replies = []

        async def _reply(ev, text):
            m.replies.append(str(text))

        m._reply = _reply
        m._render = lambda key, **kw: (kw.get("list") if key == "msg_requests_list"
                                       else "（空）")
        m.mod = types.SimpleNamespace(display_name=lambda g: f"群{g}")
        m._fmt_request_line = lambda idx, r: (
            f"{idx}. {r.get('username') or r.get('member_openid')}"
        )
        m._request_brief = lambda r: str(r.get("username") or r.get("member_openid"))
        m._managed_groups = lambda: dict(m._known or {})
        m._known = {}
        m._list_requests_all_groups = types.MethodType(
            QQGroupManager._list_requests_all_groups, m
        )
        return m

    def _ev(self, gid="g1"):
        return types.SimpleNamespace(get_group_id=lambda: gid, get_platform_id=lambda: "p1")

    def _reqs(self, n):
        return [
            {"member_openid": f"u{i}", "username": f"用户{i}", "apply_at": "", }
            for i in range(n)
        ]

    async def test_first_page_has_header_and_next_hint(self):
        m = self._mgr({"g1": self._reqs(23)})
        await QQGroupManager._cmd_list_requests(m, self._ev(), ["/审批"])
        body = m.replies[-1]
        self.assertIn("本群 23 条，第 1/3 页", body)
        self.assertIn("/群管 审批 2", body)
        self.assertIn("1. 用户0", body)

    async def test_page_keeps_global_index(self):
        m = self._mgr({"g1": self._reqs(23)})
        await QQGroupManager._cmd_list_requests(m, self._ev(), ["/审批", "2"])
        body = m.replies[-1]
        self.assertIn("第 2/3 页", body)
        self.assertIn("11. 用户10", body, "序号必须全局连续，否则 /通过 N 对不上")

    async def test_page_clamped(self):
        m = self._mgr({"g1": self._reqs(5)})
        await QQGroupManager._cmd_list_requests(m, self._ev(), ["/审批", "99"])
        self.assertIn("第 1/1 页", m.replies[-1])

    async def test_all_groups_summary(self):
        m = self._mgr({"g1": self._reqs(2), "g2": self._reqs(1)})
        m._known = {"g1": "p1", "g2": "p1"}
        await QQGroupManager._cmd_list_requests(m, self._ev(), ["/审批", "全部"])
        body = m.replies[-1]
        self.assertIn("全部群汇总", body)
        self.assertIn("共 3 条", body)
        self.assertIn("【群g1】2 条", body)
        self.assertIn("【群g2】1 条", body)

    async def test_empty(self):
        m = self._mgr({})
        await QQGroupManager._cmd_list_requests(m, self._ev(), ["/审批"])
        self.assertEqual(m.replies[-1], "（空）")


class WebRequestsToleranceTest(unittest.IsolatedAsyncioTestCase):
    """/requests 出了任何意外都要返回可读信息，不能 500。"""

    async def test_unexpected_error_returns_payload_not_500(self):
        mod = sys.modules[QQGroupManager.__module__]
        origin = mod.json_response
        mod.json_response = lambda d: d
        try:
            m = types.SimpleNamespace()
            m.logger = _Logger()

            async def boom():
                raise RuntimeError("boom")

            m._web_requests_impl = boom
            res = await QQGroupManager.web_requests(m)
        finally:
            mod.json_response = origin
        self.assertEqual(res["error"], "requests_failed")
        self.assertEqual(res["requests"], [])
        self.assertTrue(res["errors"], "至少要告诉前端是哪一步挂了")
        self.assertTrue(
            any(lvl == "error" for lvl, _ in m.logger.lines), "必须打 traceback 日志"
        )


class GroupEntriesToleranceTest(unittest.TestCase):
    """单个群元数据坏掉，不能连累整个群列表。"""

    def test_bad_group_meta_does_not_break_list(self):
        m = types.SimpleNamespace()
        m.logger = _Logger()
        m._known_groups = {"g1": "p1", "g2": "p2"}

        def _meta(g):
            if g == "g1":
                raise ValueError("坏了")
            return {"name": "群二", "alias": "二号"}

        m.mod = types.SimpleNamespace(
            get_group_meta=_meta, display_name=lambda g: {"g2": "群二"}.get(g, "")
        )
        m._bot_label = lambda pid: {"display": f"bot-{pid}"}
        out = QQGroupManager._group_entries(m)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["group_openid"], "g1")
        self.assertEqual(out[0]["bot"], "bot-p1")
        self.assertEqual(out[1]["display"], "群二")


if __name__ == "__main__":
    unittest.main()
