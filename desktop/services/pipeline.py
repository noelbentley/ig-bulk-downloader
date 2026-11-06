"""Persisted opt-in settings for the independent public-media scheduler."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineConfig:
    enabled: bool = False
    workers: int = 3

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("The optimized pipeline must be enabled or disabled.")
        if type(self.workers) is not int or not 1 <= self.workers <= 5:
            raise ValueError("Choose between one and five public media workers.")
