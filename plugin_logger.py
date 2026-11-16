"""兼容 shim：实现已迁到 qgm.integrations.plugin_logger，旧导入路径保持不变。"""

import sys as _sys

try:  # 以包方式加载（AstrBot）
    from .qgm.integrations import plugin_logger as _impl
except ImportError:  # 测试脚本把插件目录直接加进 sys.path 的情况
    from qgm.integrations import plugin_logger as _impl

_sys.modules[__name__] = _impl
