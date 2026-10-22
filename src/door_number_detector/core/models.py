"""Shared typed data models for detector workflows."""

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DetectionResult:
    success: bool
    latitude: float
    longitude: float
    door_number: str | None = None
    confidence: int = 0
    heading: int | float | None = None
    pitch: int | None = None
    observation: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)