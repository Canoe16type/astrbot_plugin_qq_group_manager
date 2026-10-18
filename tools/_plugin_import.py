"""按实际目录名导入插件模块。

插件目录不一定叫 `astrbot_plugin_qq_group_manager`（GitHub 下 ZIP 会带 `-main` 后缀、
用户也可能改目录名），脚本里写死包名会在这些环境里直接 ImportError。
这里用目录 basename 推导包名，统一走 importlib。

用法（脚本都在 tools/ 下，Python 会自动把脚本目录放进 sys.path）：

    from _plugin_import import BASE, PKG, load
    variant = load("variant")
"""

from __future__ import annotations

import importlib
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.basename(BASE)
_PARENT = os.path.dirname(BASE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


def load(name: str):
    """导入插件内模块，如 load("variant") / load("main")。"""
    return importlib.import_module(f"{PKG}.{name}")
