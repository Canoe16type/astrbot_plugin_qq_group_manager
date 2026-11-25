"""插件级常量（运行期可确定的值）。"""

from __future__ import annotations

import re
from pathlib import Path


def plugin_root() -> Path:
    """插件根目录（含 metadata.yaml 的那一层），与安装目录名无关。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "metadata.yaml").exists():
            return parent
    return here.parents[1]


def plugin_name() -> str:
    """安装目录名（市场安装时可能带哈希后缀），与 main.PLUGIN_NAME 的推导一致。"""
    return plugin_root().name


PLUGIN_NAME = plugin_name()


def _read_version() -> str:
    here = Path(__file__).resolve()
    for parent in here.parents:
        meta = parent / "metadata.yaml"
        if meta.exists():
            try:
                for line in meta.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("version:"):
                        return line.split(":", 1)[1].strip().strip('"\'')
            except OSError:
                # noqa: silent-cleanup —— 解析/读取兜底，失败即跳过
                pass
    return "0.0.0"


PLUGIN_VERSION = _read_version()


WEB_API_PREFIX = PLUGIN_NAME

LEXICON_UPSTREAM_NAME = "上游词库（一键获取）"

LEXICON_UPSTREAM_URL = ""

LEXICON_UPSTREAM_MAX_BYTES = 2 * 1024 * 1024      # 单次下载上限 2 MB

LEXICON_UPSTREAM_TIMEOUT = 15.0                   # 下载超时（秒）

LEXICON_UPSTREAM_FAST_TIMEOUT = 8.0               # 配了加速地址时，直连只等这么久

REGISTERED_COMMANDS = (
    "/记录",
    "禁言", "审批", "状态", "撤回", "审核", "群规", "撤销",
    "管理登记", "群管", "举报", "成员", "帮助",
)

_RULE_SEP_RE = re.compile(r"\s*[｜|︱]\s*")


ADMIN_COMMANDS = ("/禁言", "/审批", "/状态", "/撤回", "/审核", "/撤销", "/管理登记", "/成员", "/踢人")
