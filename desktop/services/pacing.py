"""Supplemental scheduling. This never changes Instaloader's request rate rules."""
from dataclasses import dataclass
import random
from contextlib import nullcontext


@dataclass(frozen=True)
class PacingConfig:
    enabled: bool = True
    batch_min: int = 3
    batch_max: int = 8
    pause_min: int = 15
    pause_max: int = 45
    long_rest_enabled: bool = True
    long_interval_min: int = 40
    long_interval_max: int = 60
    long_pause_min: int = 120
    long_pause_max: int = 300

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.long_rest_enabled) is not bool:
            raise ValueError("Pacing toggles must be enabled or disabled.")
        for low, high, label, ceiling in (
            (self.batch_min, self.batch_max, "Batch size", 100000),
            (self.pause_min, self.pause_max, "Batch pause", 86400),
            (self.long_interval_min, self.long_interval_max, "Long-rest interval", 100000),
            (self.long_pause_min, self.long_pause_max, "Long-rest duration", 86400),
        ):
            if type(low) is not int or type(high) is not int or not 1 <= low <= high <= ceiling:
                raise ValueError(f"{label}: enter whole numbers from 1 to {ceiling}, with minimum ≤ maximum.")


class Pacer:
    def __init__(self, config, rng=None):
        self.config = config
        self.rng = rng if rng is not None else random.Random()
        self.batch_count = self.long_count = 0
        self.pending_kind = None
        self.remaining = 0.0
        self.statistics = None
        self.batch_target = self._batch() if config.enabled else None
        self.long_target = self._interval() if config.enabled and config.long_rest_enabled else None

    def _batch(self):
        return self.rng.randint(self.config.batch_min, self.config.batch_max)

    def _interval(self):
        return self.rng.randint(self.config.long_interval_min, self.config.long_interval_max)

    def completed_file(self, control, report):
        if not self.config.enabled:
            return
        self.batch_count += 1
        self.long_count += 1
        # A long rest replaces a simultaneous batch pause and starts a fresh batch.
        if self.long_target is not None and self.long_count >= self.long_target:
            self.pending_kind = "Long rest"
            self.remaining = self.rng.randint(self.config.long_pause_min, self.config.long_pause_max)
        elif self.batch_count >= self.batch_target:
            self.pending_kind = "Batch pause"
            self.remaining = self.rng.randint(self.config.pause_min, self.config.pause_max)
        self.resume_pending(control, report)

    def resume_pending(self, control, report):
        if self.pending_kind is None:
            return
        # Keep the remaining rest if just this profile is cancelled and the queue continues.
        with self.statistics.measure("pacing_seconds") if self.statistics else nullcontext():
            control.wait(self.remaining, self.pending_kind, report, self._remaining_changed)
        if self.pending_kind == "Long rest":
            self.long_count = 0
            self.long_target = self._interval()
        self.batch_count = 0
        self.batch_target = self._batch()
        self.pending_kind = None
        self.remaining = 0.0

    def _remaining_changed(self, seconds):
        self.remaining = seconds
