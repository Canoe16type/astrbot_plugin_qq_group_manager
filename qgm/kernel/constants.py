"""插件级常量（运行期可确定的值）。"""

from __future__ import annotations

from pathlib import Path


def plugin_name() -> str:
    """安装目录名（市场安装时可能带哈希后缀），与 main.PLUGIN_NAME 的推导一致。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "metadata.yaml").exists():
            return parent.name
    return "astrbot_plugin_qq_group_manager"


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
                pass
    return "0.0.0"


PLUGIN_VERSION = _read_version()
