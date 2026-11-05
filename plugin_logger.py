"""插件日志系统：内存环形缓冲 + 可选文件落盘，供 WebUI 实时查询与 SSE 推送。

内存策略：内存里只保留最近 max_entries 条（默认 300），仅用于 SSE 实时追加；
面板上的历史检索、关键字搜索一律走磁盘文件（从文件尾按块倒序读，不整文件载入内存），
所以这台机器有多少内存都不会因为日志量变大而变吃紧。

落盘策略：按天一个文件 <插件id>_YYYYMMDD.log（即 astrbot_plugin_qq_group_manager_YYYYMMDD.log），只追加。
通过 keep_days 控制磁盘留存天数（0＝永久保留），超期文件自动删除；
清理时机＝插件启动 + 运行中每小时最多一次 + 配置热更新时（force）。
"""

import json
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

DEFAULT_MAX_ENTRIES = 300       # 内存实时缓冲默认条数（历史查询走磁盘，不需要大）
READ_CHUNK = 64 * 1024          # 倒序读日志文件的块大小（64 KB）
MAX_SCAN_BYTES = 8 * 1024 * 1024  # 单次检索最多回扫的字节数，防止超大文件拖住
MAX_MSG_CHARS = 8000            # 单条日志最大字符数（超长 traceback 截断，别撑内存）
FLUSH_INTERVAL = 2.0            # 磁盘写入合并间隔（秒）
FLUSH_BATCH = 200               # 攒够这么多条立即落盘
HEADER_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\] \[(DEBUG|INFO|WARNING|ERROR)\] ?(.*)$", re.S)


class PluginLogger:
    """线程安全的环形缓冲日志器。"""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES, level: str = "DEBUG",
                 to_file: bool = True, log_dir: str | None = None,
                 keep_days: int = DEFAULT_KEEP_DAYS, plugin_name: str | None = None):
        # 日志文件前缀跟插件 id 对齐（卸载时同目录一并删除，不会留下孤儿日志）
        self.plugin_name = plugin_name or "astrbot_plugin_qq_group_manager"
        self._lock = threading.RLock()
        self.max_entries = max(100, int(max_entries))
        self.level = LEVELS.get(str(level).upper(), 10)
        self.buffer: deque = deque(maxlen=self.max_entries)
        self._index = 0
        # 待落盘的行缓冲：合并写、减少小文件 IO 次数
        self._pending: list[str] = []
        self._pending_bytes = 0
        self._last_flush = time.time()
        self._closed = False
        self._flusher = threading.Thread(
            target=self._flush_loop, name="qgm-log-flush", daemon=True)
        self._flusher.start()
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
        if len(msg) > MAX_MSG_CHARS:
            msg = msg[:MAX_MSG_CHARS] + f"…（日志过长已截断，原 {len(msg)} 字符）"
        entry = {
            "index": self._index,
            "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "ts": ts,
            "level": LEVEL_NAMES.get(lv, "INFO"),
            "msg": msg,
        }
        self._index += 1
        # 内存只留最近 max_entries 条，超出自动挤掉最老的（不参与历史检索）
        self.buffer.append(entry)
        if self.to_file and self.file_path:
            self._pending.append(f"[{entry['time']}] [{entry['level']}] {msg}\n")
            self._pending_bytes += len(msg) + 40
            if len(self._pending) >= FLUSH_BATCH or self._pending_bytes >= 256 * 1024:
                self.flush()
        return entry

    # ---------------- 落盘（合并写，减少小 IO） ----------------

    def _flush_loop(self) -> None:
        """后台线程：每 FLUSH_INTERVAL 秒把攒下的行合并写一次。"""
        while not self._closed:
            time.sleep(FLUSH_INTERVAL)
            try:
                if self._pending:
                    self.flush()
            except Exception:
                pass

    def flush(self) -> None:
        """把待写队列合并写入当天日志文件（线程安全，可随时调）。"""
        with self._lock:
            if not self._pending or not self.file_path:
                return
            lines = "".join(self._pending)
            self._pending.clear()
            self._pending_bytes = 0
            path = self.file_path
            self._last_flush = time.time()
        try:
            self.cleanup_old_files()   # 内部有节流，每小时最多真扫一次目录
            with open(path, "a", encoding="utf-8") as f:
                f.write(lines)
            if not self._perm_fixed:
                # 新建的日志文件收紧到 0600；每个文件只做一次
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                self._perm_fixed = True
        except Exception:
            pass

    def close(self) -> None:
        """插件禁用/重载时调用：停掉后台线程并补最后一次落盘。"""
        self._closed = True
        self.flush()

    # ---------------- 磁盘检索（从文件尾倒序按块读） ----------------

    def _read_tail_lines(self, path: str):
        """从文件末尾往前按块读取，逐行 yield（最新在前，不整文件载入内存）。"""
        try:
            f = open(path, "rb")
        except Exception:
            return
        with f:
            try:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
            except Exception:
                return
            buf = b""
            scanned = 0
            while pos > 0 and scanned < MAX_SCAN_BYTES:
                step = min(READ_CHUNK, pos)
                pos -= step
                scanned += step
                try:
                    f.seek(pos)
                    chunk = f.read(step)
                except Exception:
                    return
                buf = chunk + buf
                parts = buf.split(b"\n")
                buf = parts[0]          # 首段可能是半行，留到下一轮拼接
                for raw in reversed(parts[1:]):
                    if raw:
                        yield raw.decode("utf-8", "replace")
            if buf:
                yield buf.decode("utf-8", "replace")

    @staticmethod
    def _parse_ts(text: str) -> float:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").timestamp()
        except Exception:
            return 0.0

    def _iter_disk_entries(self):
        """按文件从新到旧、文件内从新到旧，产出完整日志条目（多行 traceback 会拼回一条）。"""
        for path in reversed(self._log_files()):
            pending: list = []      # 逆序读到的续行，暂存到遇上头部行
            for line in self._read_tail_lines(path):
                m = HEADER_RE.match(line)
                if m:
                    msg = m.group(3)
                    if pending:
                        msg = msg + "\n" + "\n".join(reversed(pending))
                        pending = []
                    yield {"index": -1, "time": m.group(1),
                           "ts": self._parse_ts(m.group(1)),
                           "level": m.group(2), "msg": msg}
                else:
                    pending.append(line)

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
        """返回 index 之后的新日志（供 SSE 增量推送）。

        buffer 里 index 单调递增，直接按偏移切片即可，不再每次全量过滤——
        SSE 每来一条新日志都会调一次，旧实现是 O(n) 扫全缓冲。
        """
        with self._lock:
            if not self.buffer:
                return []
            first = self.buffer[0]["index"]
            if index < first - 1:
                # 客户端落后到已被挤出的区间，补齐不了，直接给现有全部
                return list(self.buffer)
            items = list(self.buffer)
            return items[max(0, index - first + 1):]

    def search(self, level: str = "ALL", keyword: str = "", limit: int = 200) -> list:
        """按级别与关键字检索日志（倒序返回最新在前）。

        历史检索走磁盘：先把待写队列落盘，再从最新文件尾部按块倒序扫，
        命中够数就停，内存里那几百条只用来兜底（未开文件落盘时）。
        """
        lv_name = str(level or "ALL").upper()
        kw = (keyword or "").strip().lower()
        limit = max(1, int(limit))
        want_all = lv_name in ("", "ALL")
        out: list = []

        if self.to_file:
            self.flush()            # 保证磁盘上是最新的
            for e in self._iter_disk_entries():
                if not want_all and e["level"] != lv_name:
                    continue
                if kw and kw not in e["msg"].lower():
                    continue
                out.append(e)
                if len(out) >= limit:
                    return out

        # 未开文件落盘，或磁盘文件被清空：用内存缓冲兜底
        with self._lock:
            items = list(self.buffer)
        seen = {(e["time"], e["msg"][:80]) for e in out}
        for e in reversed(items):
            if not want_all and e["level"] != lv_name:
                continue
            if kw and kw not in e["msg"].lower():
                continue
            if (e["time"], e["msg"][:80]) in seen:
                continue
            out.append(e)
            if len(out) >= limit:
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
        with self._lock:
            self._pending.clear()
            self._pending_bytes = 0
        removed = 0
        for p in self._log_files():
            try:
                os.remove(p)
                removed += 1
            except Exception:
                pass
        return removed

    def buffer_bytes(self) -> int:
        """内存缓冲区当前占用（近似值：按每条日志 JSON 序列化后的字节数累加）。

        buffer 会被其它线程 append / 自动挤出，这里先取快照再算，避免迭代时被改。
        """
        try:
            snapshot = list(self.buffer)
        except Exception:
            return 0
        total = 0
        for e in snapshot:
            try:
                total += len(json.dumps(e, ensure_ascii=False).encode("utf-8"))
            except Exception:
                total += 160  # 序列化失败时按单条平均开销估算
        return total

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
            "buffer_entries": len(self.buffer),
            "buffer_max": self.max_entries,
            "buffer_bytes": self.buffer_bytes(),
        }

    @property
    def last_index(self) -> int:
        with self._lock:
            return self._index - 1
