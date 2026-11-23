"""插件处理记录：统一查询层。"""

from .query import KINDS, PREFIX, RecordRow, get, iter_rows, query

__all__ = ["KINDS", "PREFIX", "RecordRow", "get", "iter_rows", "query"]
