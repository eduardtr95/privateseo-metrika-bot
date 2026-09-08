"""Bounded work and per-user serialization; no network calls at import time."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


class KeyedQueue:
    def __init__(self, workers: int, capacity: int, name: str):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.slots = threading.BoundedSemaphore(capacity)
        self.lock = threading.Lock()
        self.pending: set[int] = set()

    def submit(self, key: int, work) -> bool:
        with self.lock:
            if key in self.pending or not self.slots.acquire(blocking=False):
                return False
            self.pending.add(key)

        def release(_):
            with self.lock:
                self.pending.discard(key)
                self.slots.release()

        try:
            future = self.executor.submit(work)
        except RuntimeError:
            release(None)
            return False
        future.add_done_callback(release)
        return True

    def stop(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


class UserLocks:
    # Fixed stripes bound memory even for unbounded external chat IDs.
    def __init__(self):
        self.locks = [threading.RLock() for _ in range(257)]

    def for_user(self, chat_id: int):
        return self.locks[chat_id % len(self.locks)]


class RequestGate:
    """At most two API calls, <=15/s, <=180 reports/5 min per access token."""

    def __init__(self):
        self.slots = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.history: dict[str, list[float]] = {}

    @contextmanager
    def enter(self, key: str, report: bool):
        with self.slots:
            while True:
                with self.lock:
                    now = time.monotonic()
                    self.history = {
                        k: [t for t in ts if t > now - 300]
                        for k, ts in self.history.items()
                        if ts and ts[-1] > now - 300
                    }
                    history = self.history.get(key, [])
                    delay = max(0, self.next_request - now)
                    if report and len(history) >= 180:
                        # Defer instead of occupying a worker for minutes.
                        raise QuotaWait(max(1, int(history[0] + 301 - now)))
                    if delay <= 0:
                        self.next_request = now + 1 / 15
                        if report:
                            self.history.setdefault(key, []).append(now)
                        break
                time.sleep(delay)
            yield


class QuotaWait(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__("API quota cooldown")
