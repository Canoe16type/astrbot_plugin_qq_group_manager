"""入群审批策略（join_approval_strategy）离线单元测试。

跑法（在插件目录下）：
    python3 -m unittest discover -s tests -v

特点：完全离线 —— 把 QQApi._request 换成记录器，不发真实请求、不消耗接口配额，
只校验「参数怎么组装、路径/方法对不对、本地校验有没有拦住非法输入」。
真实接口能不能通，得在沙箱群手动联调（见文末 checklist）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qq_api import QQApi, QQApiError  # noqa: E402


class FakeApi(QQApi):
    """把网络层摘掉的 QQApi：记录调用，可指定返回/抛错。"""

    def __init__(self):
        super().__init__("appid_test", "secret_test")
        self.calls = []
        self.next_response = {"ok": True}
        self.next_error = None

    async def _request(self, method, path, params=None, body=None):
        self.calls.append({"method": method, "path": path, "params": params, "body": body})
        if self.next_error is not None:
            raise self.next_error
        return self.next_response


class StrategyQueryTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_default_and_paging(self):
        api = FakeApi()
        await api.list_join_approval_strategies()
        call = api.calls[-1]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["path"], "/v2/groups/join_approval_strategy")
        self.assertEqual(call["params"], {"limit": 20})  # 默认 20，且不带空 cursor
        self.assertIsNone(call["body"])

        await api.list_join_approval_strategies(cursor="abc", limit=10)
        self.assertEqual(api.calls[-1]["params"], {"limit": 10, "cursor": "abc"})

    async def test_list_limit_is_clamped(self):
        """现状：0/None 视为"没传"回落 20，上界裁到 50（官方 page_size 上限）。"""
        api = FakeApi()
        await api.list_join_approval_strategies(limit=0)
        self.assertEqual(api.calls[-1]["params"]["limit"], 20)
        await api.list_join_approval_strategies(limit=None)
        self.assertEqual(api.calls[-1]["params"]["limit"], 20)
        await api.list_join_approval_strategies(limit=999)
        self.assertEqual(api.calls[-1]["params"]["limit"], 50)
        await api.list_join_approval_strategies(limit=5)
        self.assertEqual(api.calls[-1]["params"]["limit"], 5)

    async def test_list_passes_through_response(self):
        api = FakeApi()
        api.next_response = {"strategies": [{"strategy_id": "s1"}], "next_cursor": ""}
        data = await api.list_join_approval_strategies()
        self.assertEqual(data["strategies"][0]["strategy_id"], "s1")


class StrategyCreateTest(unittest.IsolatedAsyncioTestCase):
    async def test_requires_exactly_one_of_two_id_forms(self):
        api = FakeApi()
        with self.assertRaises(QQApiError):
            await api.create_join_approval_strategy()  # 都没传
        with self.assertRaises(QQApiError):
            await api.create_join_approval_strategy(group_openids=["A"], group_ids=[1])
        self.assertEqual(api.calls, [])  # 本地就拦了，不该发出请求

    async def test_openids_mode_body(self):
        api = FakeApi()
        await api.create_join_approval_strategy(
            group_openids=["G1", "G2"], expire_at="2027-01-01T00:00:00+08:00", remark="全量放行"
        )
        body = api.calls[-1]["body"]
        self.assertEqual(api.calls[-1]["method"], "POST")
        self.assertEqual(body["group_openids"], ["G1", "G2"])
        self.assertNotIn("group_ids", body)
        self.assertEqual(body["is_enable"], "on")
        self.assertEqual(body["remark"], "全量放行")

    async def test_invalid_is_enable_falls_back_to_on(self):
        api = FakeApi()
        await api.create_join_approval_strategy(group_ids=[123], is_enable="maybe")
        self.assertEqual(api.calls[-1]["body"]["is_enable"], "on")

    async def test_truncates_oversized_inputs(self):
        api = FakeApi()
        await api.create_join_approval_strategy(
            group_openids=[f"G{i}" for i in range(150)], remark="x" * 300
        )
        body = api.calls[-1]["body"]
        self.assertEqual(len(body["group_openids"]), 100)   # 官方上限 100 个群/次
        self.assertEqual(len(body["remark"]), 255)


class StrategyUpdateDeleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_update_requires_id_and_body(self):
        api = FakeApi()
        with self.assertRaises(QQApiError):
            await api.update_join_approval_strategy("")
        with self.assertRaises(QQApiError):
            await api.update_join_approval_strategy("s1")  # 一个字段都没传

    async def test_update_group_action_validation(self):
        api = FakeApi()
        with self.assertRaises(QQApiError):
            await api.update_join_approval_strategy("s1", group_action={"op": "modify"})

        await api.update_join_approval_strategy(
            "s1", is_enable="off", group_action={"op": "del", "group_openids": ["G1"]}
        )
        call = api.calls[-1]
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(call["path"], "/v2/groups/join_approval_strategy/s1")
        self.assertEqual(call["body"]["is_enable"], "off")
        self.assertEqual(call["body"]["group_action"]["op"], "del")

    async def test_delete_and_execute_paths(self):
        api = FakeApi()
        await api.delete_join_approval_strategy("s1")
        self.assertEqual(api.calls[-1]["method"], "DELETE")
        self.assertEqual(api.calls[-1]["path"], "/v2/groups/join_approval_strategy/s1")

        await api.execute_join_approval_strategy("s1")
        self.assertEqual(api.calls[-1]["method"], "POST")
        self.assertTrue(api.calls[-1]["path"].endswith("/join_approval_strategy/s1/execute"))

    async def test_empty_id_rejected(self):
        api = FakeApi()
        for fn in (api.delete_join_approval_strategy, api.execute_join_approval_strategy):
            with self.assertRaises(QQApiError):
                await fn("")
        self.assertEqual(api.calls, [])


class WhitelistTest(unittest.IsolatedAsyncioTestCase):
    async def test_add_users_stringified(self):
        api = FakeApi()
        await api.update_join_approval_whitelist("s1", "add", [" 123456 ", 987654321098765])
        body = api.calls[-1]["body"]
        self.assertEqual(api.calls[-1]["path"], "/v2/groups/join_approval_strategy/s1/whitelist_users")
        self.assertEqual(body["op"], "add")
        # 超大整数会被 str() 保留原样，不会变成科学计数法
        self.assertEqual(body["whitelist_users"], ["123456", "987654321098765"])

    async def test_bad_op_and_empty_users(self):
        api = FakeApi()
        with self.assertRaises(QQApiError):
            await api.update_join_approval_whitelist("s1", "remove", ["1"])
        with self.assertRaises(QQApiError):
            await api.update_join_approval_whitelist("s1", "add", ["  ", ""])
        with self.assertRaises(QQApiError):
            await api.update_join_approval_whitelist("s1", "add", [str(i) for i in range(10001)])
        self.assertEqual(api.calls, [])

    async def test_whitelist_does_not_filter_non_numeric(self):
        """当前实现只做去空白，纯数字校验在面板层做 —— 这里记录现状，改行为时要知道。"""
        api = FakeApi()
        await api.update_join_approval_whitelist("s1", "add", ["abc"])
        self.assertEqual(api.calls[-1]["body"]["whitelist_users"], ["abc"])


class ErrorPropagationTest(unittest.IsolatedAsyncioTestCase):
    async def test_api_error_carries_code_and_status(self):
        api = FakeApi()
        api.next_error = QQApiError("无权限", code=11253, status=403)
        with self.assertRaises(QQApiError) as ctx:
            await api.list_join_approval_strategies()
        self.assertEqual(ctx.exception.code, 11253)
        self.assertEqual(ctx.exception.status, 403)


class JoinRequestApprovalTest(unittest.IsolatedAsyncioTestCase):
    """老的单人审批接口：保证 approve / decline 两种 body 不串味。"""

    async def test_approve_body(self):
        api = FakeApi()
        await api.approve_join_request("G1", "M1", "R1", op="approve")
        self.assertEqual(api.calls[-1]["body"], {"op": "approve", "join_request_id": "R1"})
        self.assertEqual(
            api.calls[-1]["path"], "/v2/groups/G1/approval_join_request/M1"
        )

    async def test_decline_body_extras_only_on_reject(self):
        api = FakeApi()
        await api.approve_join_request(
            "G1", "M1", "R1", op="decline", reject_reason="广告", add_to_blacklist=True
        )
        body = api.calls[-1]["body"]
        self.assertEqual(body["op"], "decline")
        self.assertEqual(body["reject_reason"], "广告")
        self.assertTrue(body["add_to_member_blacklist"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
