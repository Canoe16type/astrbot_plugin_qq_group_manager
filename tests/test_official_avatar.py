"""头像 URL：官方事件优先，旧 qlogo 回退。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from plugins.astrbot_plugin_qq_group_manager.main import QQGroupManager  # noqa: E402


class _Raw:
    def __init__(self, author):
        self.author = author
        self.mentions = []


class _Msg:
    def __init__(self, author):
        self.raw_message = _Raw(author)


class _Event:
    def __init__(self, author):
        self.message_obj = _Msg(author)

    def get_platform_id(self): return "qq_official_1"
    def get_sender_id(self): return "u1"


class OfficialAvatarTest(unittest.TestCase):
    def setUp(self):
        self.p = object.__new__(QQGroupManager)
        self.p._platform_appid = lambda pid: "1020"

    def test_official_avatar_is_used(self):
        ev = _Event({"member_openid": "u1", "avatar": "https://official/a.png"})
        out = self.p._expand_msg_modules("{avatar}", ev, "markdown")
        self.assertIn("https://official/a.png", out)

    def test_fallback_to_qlogo_when_missing(self):
        ev = _Event({"member_openid": "u1"})
        out = self.p._expand_msg_modules("{avatar}", ev, "markdown")
        self.assertIn("q.qlogo.cn/qqapp/1020/u1/", out)


if __name__ == "__main__":
    unittest.main()
