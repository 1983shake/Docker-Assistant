"""运行时核心：日志处理器 + 任务进度跟踪。"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import DATA_DIR

# ============================================================
#  Part 1: 日志处理器（内存环形缓冲 + JSONL 持久化）
# ============================================================

LOG_FILE = Path(DATA_DIR) / "logs.jsonl"
LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}

_NOISY_LOGGERS = (
    "docker",
    "urllib3",
    "requests",
    "httpcore",
    "httpx",
    "charset_normalizer",
    "asyncio",
    "watchfiles",
    "python_multipart",
    "uvicorn.access",
)


class _NoisyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        return not any(name == p or name.startswith(p + ".") for p in _NOISY_LOGGERS)


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity=2000, log_file=None, max_entries=5000, retention_days=7):
        super().__init__(level=logging.DEBUG)
        self._buffer: deque = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._seq = 0
        self._log_file = Path(log_file) if log_file else None
        self._max_entries = max(100, int(max_entries))
        self._retention_days = max(1, int(retention_days))
        self._file_lock = threading.RLock()
        self._load_from_file()

    def _load_from_file(self) -> None:
        if not self._log_file or not self._log_file.exists():
            return
        try:
            with self._file_lock:
                lines = self._log_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        if self._max_entries and len(lines) > self._max_entries:
            lines = lines[-self._max_entries :]
        with self._lock:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                self._seq += 1
                rec["seq"] = self._seq
                self._buffer.append(rec)

    def _append_to_file(self, entry: Dict[str, Any]) -> None:
        if not self._log_file:
            return
        try:
            with self._file_lock:
                self._log_file.parent.mkdir(parents=True, exist_ok=True)
                with self._log_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "ts": float(record.created),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
            with self._lock:
                self._seq += 1
                entry["seq"] = self._seq
                self._buffer.append(entry)
            self._append_to_file(entry)
        except Exception:
            self.handleError(record)

    def snapshot(self, after: int = 0, limit: int = 500, min_level: str = "INFO") -> Dict[str, Any]:
        rank = LOG_LEVELS.get((min_level or "INFO").upper(), 1)
        with self._lock:
            items = [it for it in self._buffer if it["seq"] > after and LOG_LEVELS.get(it["level"], 1) >= rank][-limit:]
            return {"logs": items, "last_seq": self._seq}

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
        if self._log_file:
            try:
                with self._file_lock:
                    if self._log_file.exists():
                        self._log_file.write_text("", encoding="utf-8")
            except Exception:
                pass

    def cleanup_file(self, retention_days: int = 7, max_entries: int = 5000) -> Dict[str, Any]:
        retention_days = max(1, int(retention_days))
        max_entries = max(100, int(max_entries))
        cutoff = time.time() - retention_days * 86400
        result = {"read": 0, "kept": 0, "removed_expired": 0, "removed_overflow": 0}
        if not self._log_file or not self._log_file.exists():
            return result
        try:
            with self._file_lock:
                lines = self._log_file.read_text(encoding="utf-8").splitlines()
            result["read"] = len(lines)
            kept: List[str] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts")
                if isinstance(ts, (int, float)) and ts < cutoff:
                    result["removed_expired"] += 1
                    continue
                kept.append(line)
            if len(kept) > max_entries:
                result["removed_overflow"] = len(kept) - max_entries
                kept = kept[-max_entries:]
            result["kept"] = len(kept)
            with self._file_lock:
                tmp = self._log_file.with_suffix(".jsonl.tmp")
                tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                tmp.replace(self._log_file)
            with self._lock:
                new_buf: deque = deque(maxlen=self._buffer.maxlen)
                for it in self._buffer:
                    ts = it.get("ts")
                    if isinstance(ts, (int, float)) and ts < cutoff:
                        continue
                    new_buf.append(it)
                self._buffer = new_buf
        except Exception:
            pass
        return result


log_handler = MemoryLogHandler(
    capacity=2000,
    log_file=LOG_FILE,
    max_entries=5000,
    retention_days=7,
)
log_handler.setLevel(logging.DEBUG)
log_handler.setFormatter(logging.Formatter("%(message)s"))
log_handler.addFilter(_NoisyFilter())


# ============================================================
#  Part 2: 任务进度跟踪
# ============================================================


class ProgressTracker:
    """内存态任务进度，供 Web 实时反馈。

    「已完成任务」保留时长由 config.updater.progress_popup_duration 控制。
    """

    def __init__(self) -> None:
        self._progress: dict[str, dict] = {}

    def _ttl(self) -> int:
        try:
            from app.config import config  # 延迟导入避免循环

            return max(1, int(getattr(config.updater, "progress_popup_duration", 3) or 3))
        except Exception:
            return 3

    def start(self, task: str, label: str, total: int = 0, message: str = ""):
        self._progress[task] = {
            "label": label,
            "running": True,
            "done": 0,
            "total": total,
            "message": message,
            "percent": 0,
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    def update(self, task: str, **kwargs):
        entry = self._progress.get(task)
        if entry is None:
            entry = {
                "label": task,
                "running": True,
                "done": 0,
                "total": 0,
                "message": "",
                "percent": 0,
                "started_at": time.time(),
            }
            self._progress[task] = entry
        entry.update(kwargs)
        total = entry.get("total") or 0
        done = entry.get("done") or 0
        entry["percent"] = int(done / total * 100) if total > 0 else 0
        entry["updated_at"] = time.time()

    def finish(self, task: str, message: str = "完成"):
        entry = self._progress.get(task)
        if entry is None:
            return
        entry["running"] = False
        entry["message"] = message
        entry["percent"] = 100
        entry["updated_at"] = time.time()

    def get_all(self) -> dict:
        now = time.time()
        ttl = self._ttl()
        for k in list(self._progress.keys()):
            v = self._progress[k]
            if not v.get("running") and now - v.get("updated_at", 0) > ttl:
                self._progress.pop(k, None)
        return {k: dict(v) for k, v in self._progress.items()}


progress = ProgressTracker()
