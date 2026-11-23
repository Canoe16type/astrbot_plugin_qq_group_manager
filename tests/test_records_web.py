"""记录 Web API。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.qgm.features.records import web  # noqa: E402


class _Query(dict):
    def get(self, key, default=None, type=None):
        v = dict.get(self, key, default)
        return type(v) if type and v is not None else v


class _Req:
    query = _Query({"group": "g", "limit": "20"})


def _host():
    return SimpleNamespace(
        mod=SimpleNamespace(list_verdicts=lambda group="", limit=100: []),
        _mute_history={"g": []}, _recall_history={"g": []},
        _plugin_data_dir=lambda: "/tmp/nonexistent-qgm",
    )


class RecordsWebTest(unittest.TestCase):
    def test_recall_detail_keeps_content(self):
        host = _host()
        host._recall_history = {"g": [{
            "id": 1, "group_openid": "g", "recalled_at": 100,
            "target_openid": "u1", "target_name": "小明",
            "content": "原始消息", "message_id": "m1", "event_id": "e1",
        }]}
        class Req:
            query = _Query({"group": "g", "id": "R1"})
        with patch.object(web, "request", Req()), \
             patch.object(web, "json_response", lambda d, **k: d):
            out = asyncio.run(web.web_records_detail(host))
        self.assertEqual(out["item"]["content"], "原始消息")

    def test_list_returns_json(self):
        with patch.object(web, "request", _Req()), \
             patch.object(web, "json_response", lambda d, **k: d):
            out = asyncio.run(web.web_records(_host()))
        self.assertIn("items", out)
        self.assertIn("coverage", out)


if __name__ == "__main__":
    unittest.main()
