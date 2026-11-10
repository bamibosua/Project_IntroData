"""Cooldown deadline shared by the arXiv metadata and source clients."""
import threading
import time


class SharedCooldown:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self._until = 0.0
        self._lock = threading.Lock()

    def defer(self, seconds):
        with self._lock:
            self._until = max(self._until, self.clock() + seconds)

    def remaining(self):
        with self._lock:
            return max(0.0, self._until - self.clock())

    def wait(self):
        while (remaining := self.remaining()) > 0:
            self.sleep(min(60.0, remaining))
