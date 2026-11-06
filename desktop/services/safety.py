"""Persistent restriction gate. No automatic retry, identity rotation or bypass."""
from contextlib import nullcontext
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import threading
import time

from instaloader.exceptions import AbortDownloadException, ConnectionException, TooManyRequestsException
import re
from desktop.services.control import Cancelled


class RestrictionStopped(AbortDownloadException):
    def __init__(self, status=429, review=False):
        self.status, self.review = status, review
        super().__init__("Manual review required before more downloads." if review else
                         f"HTTP {status}: requests stopped; restriction cooldown applies.")


def retry_after_seconds(value, now=None):
    now = time.time() if now is None else now
    if value is None:
        return None
    try:
        number = int(str(value).strip())
        return number if number >= 0 else None
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, date.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None


class RestrictionGate:
    def __init__(self, state=None, wall=time.time, monotonic=time.monotonic):
        self.wall, self.monotonic = wall, monotonic
        self.lock = threading.RLock()
        self.until = 0.0
        self.deadline = 0.0
        self.events = 0
        self.review = False
        if isinstance(state, dict):
            until, events = state.get("until", 0), state.get("events", 0)
            if type(until) in (int, float) and math.isfinite(until) and until >= 0:
                self.until = float(until)
                self.deadline = monotonic() + max(0, self.until - wall())
            if type(events) is int and 0 <= events < 1000000:
                self.events = events
            self.review = state.get("review") is True or self.events >= 2

    def state(self):
        with self.lock:
            return {"until": self.until, "events": self.events, "review": self.review}

    def remaining(self):
        with self.lock:
            return max(0, self.until - self.wall(), self.deadline - self.monotonic())

    def restrict(self, seconds, report, confirmed=True, review=False):
        with self.lock:
            self.until = max(self.until, self.wall() + seconds)
            self.deadline = max(self.deadline, self.monotonic() + seconds)
            if confirmed:
                self.events += 1
            self.review = self.review or review or self.events >= 2
        report("safety", self.state())

    def inspect_response(self, response, report):
        status = response.status_code
        seconds = retry_after_seconds(response.headers.get("Retry-After"), self.wall())
        if status == 429 or (status >= 400 and seconds is not None):
            self.restrict(seconds if seconds is not None else 900, report, confirmed=status == 429)
            raise RestrictionStopped(status)

    def inspect_exception(self, error, report, observed_events=None):
        if isinstance(error, RestrictionStopped):
            return
        if observed_events is not None and self.events > observed_events:
            return  # Instaloader may wrap the already observed response (e.g. test_login()).
        message = str(error).lower()
        if isinstance(error, TooManyRequestsException) or (isinstance(error, (AbortDownloadException, ConnectionException)) and re.search(r"\b429\b", message)):
            self.restrict(900, report)
        elif any(word in message for word in ("feedback_required", "checkpoint_required", "challenge_required")):
            self.restrict(900, report, review=True)

    def acknowledge_review(self):
        with self.lock:
            self.review = False
            self.events = 0  # Explicit review starts a new incident sequence, not a shorter cooldown.

    def wait(self, control, report, statistics=None):
        if self.review:
            raise RestrictionStopped(review=True)
        if self.remaining() <= 0:
            return
        with statistics.measure("cooldown_seconds") if statistics else nullcontext():
            previous = None
            while self.remaining() > 0:
                control.checkpoint(report)
                seconds = math.ceil(self.remaining())
                if seconds != previous:
                    report("state", "Restriction cooldown")
                    report("pacing", {"kind": "Restriction cooldown", "remaining": seconds, "paused": False})
                    previous = seconds
                if control.cancelled.wait(min(0.2, self.remaining())):
                    raise Cancelled()
            control.checkpoint(report)
        report("state", "Downloading")
