"""回归测试：词库上游下载的「只抓公网」护栏。

背景：护栏里原本写的是 `Main._check_url_public(...)` / `Main._ip_is_public(...)`，
而 `Main` 这个别名在重构后已经不存在了（类改名成 QQGroupManager），
pyflakes 报 undefined name，运行期只要走到这条路径就 NameError：

- `_fetch_upstream_raw()` 第一行就是 `Main._check_url_public(url)`，
  于是**上游词库下载 100% 失败**，而且外层 try/except 把 NameError 当
  "下载失败" 吞掉，日志里只看到「拉取失败」，很难定位；
- `_PublicOnlyRedirectHandler` 遇到 30x 跳转同样 NameError。

这里用「内网地址必须被拦下」把这条路径真正跑起来：
只要 NameError 还在，`_check_url_public` 会抛异常而不是返回提示语，测试即失败。

跑法（插件目录下）：
    python3 -m unittest discover -s tests -p "test_*.py"
"""

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

main = importlib.import_module("plugins.astrbot_plugin_qq_group_manager.main")
QQGroupManager = main.QQGroupManager


class UrlGuardTest(unittest.TestCase):
    def test_loopback_rejected_without_nameerror(self):
        for url in (
            "http://127.0.0.1:8080/lexicon.json",
            "http://localhost/lexicon.json",
            "http://[::1]/lexicon.json",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.5/lexicon.json",
        ):
            with self.subTest(url=url):
                self.assertTrue(
                    QQGroupManager._check_url_public(url),
                    f"{url} 属于内网/本机，必须拦下",
                )

    def test_public_url_passes(self):
        with self.subTest("公网 URL 不应被拦"):
            self.assertEqual(QQGroupManager._check_url_public("https://example.com/a.json"), "")

    def test_fetch_upstream_rejects_internal_url(self):
        """下载入口必须抛 ValueError（而不是 NameError），便于上层给人话提示。"""
        with self.assertRaises(ValueError):
            QQGroupManager._fetch_upstream_raw("http://127.0.0.1/lexicon.json")

    def test_redirect_to_internal_is_blocked(self):
        handler = main._PublicOnlyRedirectHandler()
        with self.assertRaises(ValueError):
            handler.redirect_request(
                None, None, 302, "Found", {}, "http://127.0.0.1/evil.json"
            )


if __name__ == "__main__":
    unittest.main()
