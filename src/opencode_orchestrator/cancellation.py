from __future__ import annotations

from collections.abc import Callable
import threading


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    @staticmethod
    def _run_callback(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            return

    def add_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            run_now = self._event.is_set()
            if not run_now:
                self._callbacks.append(callback)
        if run_now:
            self._run_callback(callback)

    def cancel(self, reason: str) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            self._run_callback(callback)
        return True
