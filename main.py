"""AstrBot 插件入口：实现位于 qgm/plugin.py。

保留本文件是为了兼容 AstrBot 的加载方式与既有测试的导入路径；
业务逻辑、指令与 Web API 均在 qgm/ 包内。
"""

from .qgm import plugin as _impl

# 兼容导出：测试/旧代码可能引用 main.<工具或常量>。
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)
del _name

__all__ = ["QQGroupManager"]
