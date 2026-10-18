"""插件日志系统：内存环形缓冲 + 可选文件落盘，供 WebUI 实时查询与 SSE 推送。

落盘策略：按天一个文件 <插件id>_YYYYMMDD.log（即 astrbot_plugin_qq_group_manager_YYYYMMDD.log），只追加。
通过 keep_days 控制磁盘留存天数（0＝永久保留），超期文件自动删除；
清理时机＝插件启动 + 运行中每小时最多一次 + 配置热更新时（force）。
"""

import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
LEVEL_NAMES = {10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR"}

# 日志文件名的日期部分，用于判断文件年龄（同时兼容旧文件名兜底用 mtime）
LOG_FILE_RE = re.compile(r"^(?:astrbot_plugin_qq_group_manager|qq_group_manager)_(\d{8})\.log$")
# 允许的日志文件名前缀：新前缀＝插件 id，旧前缀仅为兼容 ≤1.0.0 留下的文件
LEGACY_LOG_PREFIX = "qq_group_manager"
DEFAULT_KEEP_DAYS = 14          # 默认磁盘留存天数
CLEANUP_INTERVAL = 3600         # 运行中清理检查间隔（秒）


class PluginLogger:
    """线程安全的环形缓冲日志器。"""

    def __init__(self, max_entries: int = 2000, level: str = "DEBUG",
                 to_file: bool = True, log_dir: str | None = None,
                 keep_days: int = DEFAULT_KEEP_DAYS, plugin_name: str | None = None):
        # 日志文件前缀跟插件 id 对齐（卸载时同目录一并删除，不会留下孤儿日志）
        self.plugin_name = plugin_name or "astrbot_plugin_qq_group_manager"
        self._lock = threading.Lock()
        self.max_entries = max(100, int(max_entries))
        self.level = LEVELS.get(str(level).upper(), 10)
        self.buffer: deque = deque(maxlen=self.max_entries)
        self._index = 0
        self.to_file = to_file
        self.file_path: str | None = None
        self.keep_days = max(0, int(keep_days or 0))
        self._last_cleanup = 0.0
        self._perm_fixed = False
        self.log_dir = self._resolve_dir(log_dir)
        if self.to_file and self.log_dir:
            self.file_path = os.path.join(self.log_dir, self._today_name())
        # 启动即按留存天数清一次历史日志（即便本次未开启文件落盘，也顺手清）
        self.cleanup_old_files(force=True)

    def _today_name(self) -> str:
        return f"{self.plugin_name}_{datetime.now().strftime('%Y%m%d')}.log"

    def _resolve_dir(self, log_dir: str | None) -> str | None:
        """统一日志目录：显式传入的目录直接用它，否则用 plugin_data/<插件id>（与插件数据同目录）。"""
        try:
            base = log_dir or os.path.join(
                os.path.realpath(os.path.join(os.getcwd(), "data", "plugin_data")),
                self.plugin_name,
            )
            Path(base).mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(base, 0o700)   # 日志里有 openid / 群名，目录不给其他用户进
            except OSError:
                pass
            return base
        except Exception:
            return None

    def _log_files(self) -> list[str]:
        """列出本插件的全部日志文件路径。"""
        if not self.log_dir or not os.path.isdir(self.log_dir):
            return []
        out = []
        try:
            names = os.listdir(self.log_dir)
        except Exception:
            return []
        prefixes = (self.plugin_name + "_", LEGACY_LOG_PREFIX + "_")
        for n in names:
            if n.endswith(".log") and any(n.startswith(p) for p in prefixes):
                out.append(os.path.join(self.log_dir, n))
        return sorted(out)

    def cleanup_old_files(self, force: bool = False) -> int:
        """删除超过 keep_days 天的日志文件，返回删除数量。

        keep_days <= 0 表示永久保留（不清理）。
        非 force 时每小时最多真正检查一次，避免每条日志都扫目录。
        """
        now = time.time()
        if not force and now - self._last_cleanup < CLEANUP_INTERVAL:
            return 0
        self._last_cleanup = now
        days = int(self.keep_days or 0)
        if days <= 0:
            return 0
        cutoff = now - days * 86400
        removed = 0
        for p in self._log_files():
            ts = None
            m = LOG_FILE_RE.match(os.path.basename(p))
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y%m%d").timestamp()
                except Exception:
                    ts = None
            if ts is None:
                try:
                    ts = os.path.getmtime(p)
                except Exception:
                    continue
            if ts < cutoff:
                try:
                    os.remove(p)
                    removed += 1
                except Exception:
                    pass
        return removed

    def reconfigure(self, level: str | None = None, to_file: bool | None = None,
                    max_entries: int | None = None, log_dir: str | None = None,
                    keep_days: int | None = None, plugin_name: str | None = None) -> None:
        """运行时热更新日志配置（WebUI 设置页保存后调用，无需重启插件）。"""
        with self._lock:
            if plugin_name:
                self.plugin_name = plugin_name
            if level is not None:
                self.level = LEVELS.get(str(level).upper(), self.level)
            if keep_days is not None:
                self.keep_days = max(0, int(keep_days or 0))
            if max_entries is not None:
                new_max = max(100, int(max_entries))
                if new_max != self.max_entries:
                    kept = list(self.buffer)[-new_max:]
                    self.buffer = deque(kept, maxlen=new_max)
                    self.max_entries = new_max
            if log_dir is not None:
                self.log_dir = self._resolve_dir(log_dir) or self.log_dir
            if to_file is not None:
                if to_file and not self.file_path:
                    if self.log_dir:
                        self.file_path = os.path.join(self.log_dir, self._today_name())
                        self._perm_fixed = False
                elif not to_file:
                    self.file_path = None
                self.to_file = bool(to_file)
        # 改了留存天数立刻按新规则清一次（结果写日志，便于面板确认）
        if keep_days is not None:
            removed = self.cleanup_old_files(force=True)
            if removed:
                self.info(f"按留存策略清理了 {removed} 个历史日志文件（保留 {self.keep_days} 天）")

    def _write(self, lv: int, msg: str) -> dict:
        ts = time.time()
        entry = {
            "index": self._index,
            "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "ts": ts,
            "level": LEVEL_NAMES.get(lv, "INFO"),
            "msg": msg,
        }
        self._index += 1
        self.buffer.append(entry)
        if self.to_file and self.file_path:
            # 顺手做一次节流后的过期清理（每小时最多真正扫一次目录）
            self.cleanup_old_files()
            try:
                with open(self.file_path, "a", encoding="utf-8") as f:
                    f.write(
                        f"[{entry['time']}] [{entry['level']}] {msg}\n"
                    )
                if not self._perm_fixed:
                    # 新建的日志文件收紧到 0600；只在每个文件上做一次
                    try:
                        os.chmod(self.file_path, 0o600)
                    except OSError:
                        pass
                    self._perm_fixed = True
            except Exception:
                pass
        return entry

    def debug(self, msg: str) -> None:
        if self.level <= 10:
            with self._lock:
                self._write(10, msg)

    def info(self, msg: str) -> None:
        if self.level <= 20:
            with self._lock:
                self._write(20, msg)

    def warning(self, msg: str) -> None:
        if self.level <= 30:
            with self._lock:
                self._write(30, msg)

    def error(self, msg: str) -> None:
        if self.level <= 40:
            with self._lock:
                self._write(40, msg)

    def exception(self, msg: str) -> None:
        """等价于 Logger.exception：ERROR 级 + 完整 traceback。

        插件里多处异常兜底写的是 self.logger.exception(...)，本类早期没有这个方法，
        结果「处理异常」这行自己又抛一个 AttributeError，把真正的异常盖成
        "PluginLogger object has no attribute 'exception'"，日志里几乎看不出
        原始错误。这里补上，保证异常路径永远不会二次炸。
        """
        import traceback

        try:
            tb = traceback.format_exc()
        except Exception:  # pragma: no cover - 理论不可达
            tb = ""
        if tb and tb.strip() != "NoneType: None":
            text = msg + chr(10) + tb
        else:
            text = msg
        if self.level <= 40:
            with self._lock:
                self._write(40, text)

    def critical(self, msg: str) -> None:
        """对齐标准 Logger 的 critical；本插件按 ERROR 处理。"""
        self.error(msg)

    def get_since(self, index: int) -> list:
        """返回 index 之后的新日志（供 SSE 增量推送）。"""
        with self._lock:
            return [e for e in self.buffer if e["index"] > index]

    def search(self, level: str = "ALL", keyword: str = "", limit: int = 200) -> list:
        """按级别与关键字检索日志（倒序返回最新在前）。"""
        lv = LEVELS.get(str(level).upper(), 0)
        kw = (keyword or "").strip()
        with self._lock:
            items = list(self.buffer)
        items.reverse()
        out = []
        for e in items:
            if lv and e["level"] != level.upper():
                continue
            if kw and kw.lower() not in e["msg"].lower():
                continue
            out.append(e)
            if len(out) >= max(1, int(limit)):
                break
        return out

    def clear(self, remove_files: bool = False) -> int:
        """清空内存日志；remove_files=True 时连同磁盘日志文件一起删掉。"""
        with self._lock:
            n = len(self.buffer)
            self.buffer.clear()
        if remove_files:
            self.clear_files()
        return n

    def clear_files(self) -> int:
        """删除全部磁盘日志文件（含当天文件），返回删除数量。

        文件被删后无需处理：下次写日志时 open(..., "a") 会自动重建。
        """
        removed = 0
        for p in self._log_files():
            try:
                os.remove(p)
                removed += 1
            except Exception:
                pass
        return removed

    def log_status(self) -> dict:
        """给 WebUI 用的日志概况：目录 / 文件数 / 占用 / 留存天数。"""
        files = self._log_files()
        size = 0
        for p in files:
            try:
                size += os.path.getsize(p)
            except Exception:
                pass
        return {
            "dir": self.log_dir or "",
            "file_count": len(files),
            "file_bytes": size,
            "keep_days": self.keep_days,
            "to_file": bool(self.to_file),
        }

    @property
    def last_index(self) -> int:
        with self._lock:
            return self._index - 1
