"""Cooperative worker control, including a stopwatch that freezes on manual pause."""
import math
import threading
import time


class Cancelled(Exception):
    pass


class Control:
    def __init__(self, clock=time.monotonic):
        self.cancelled = threading.Event()
        self.resumed = threading.Event()
        self.resumed.set()
        self._wake = threading.Event()
        self._clock = clock
        self._lock = threading.Lock()
        self._paused_at = None
        self._paused_total = 0.0

    def set_paused(self, paused):
        with self._lock:
            now = self._clock()
            if paused and self._paused_at is None:
                self._paused_at = now
                self.resumed.clear()
            elif not paused and self._paused_at is not None:
                self._paused_total += now - self._paused_at
                self._paused_at = None
                self.resumed.set()
        self._wake.set()

    def active_time(self):
        with self._lock:
            now = self._clock()
            return now - self._paused_total - (now - self._paused_at if self._paused_at is not None else 0)

    def checkpoint(self, report=lambda *_: None):
        if not self.resumed.is_set() and not self.cancelled.is_set():
            report("state", "Paused")
        while not self.resumed.wait(0.1):
            if self.cancelled.is_set():
                raise Cancelled()
        if self.cancelled.is_set():
            raise Cancelled()

    def wait(self, seconds, kind, report, remaining_changed=lambda *_: None):
        """Wait in active time; cancellation wakes immediately, pause freezes the remainder."""
        start = self.active_time()
        previous = None
        while True:
            self._wake.clear()
            remaining = max(0.0, seconds - (self.active_time() - start))
            remaining_changed(remaining)
            if self.cancelled.is_set():
                raise Cancelled()
            paused = not self.resumed.is_set()
            if remaining <= 0 and not paused:
                report("state", "Downloading")
                report("activity", f"{kind} finished; resuming downloads")
                return
            display = (math.ceil(remaining), paused)
            if display != previous:
                report("state", "Paused" if paused else kind)
                report("pacing", {"kind": kind, "remaining": display[0], "paused": paused})
                previous = display
            self._wake.wait(0.1 if paused else min(0.1, remaining))

    def cancel(self):
        self.cancelled.set()
        self.resumed.set()
        self._wake.set()
