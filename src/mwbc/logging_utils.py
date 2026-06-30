from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any


class RecentLogStore(logging.Handler):
    def __init__(self, capacity: int = 300) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        item = {
            "created": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        }
        with self._lock:
            self._records.append(item)

    def snapshot(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(self.capacity, int(limit)))
        with self._lock:
            return list(self._records)[-limit:]


_RECENT_LOG_STORE = RecentLogStore()


def install_recent_log_handler() -> RecentLogStore:
    root = logging.getLogger()
    if _RECENT_LOG_STORE not in root.handlers:
        _RECENT_LOG_STORE.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(_RECENT_LOG_STORE)
    return _RECENT_LOG_STORE


def recent_logs(limit: int = 100) -> list[dict[str, Any]]:
    return _RECENT_LOG_STORE.snapshot(limit)
