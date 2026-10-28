"""Shared typed data models for detector workflows."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum


class DetectionOutcome(str, Enum):
    FOUND = "found"
    LOW_CONFIDENCE = "low_confidence"
    NO_IMAGERY = "no_imagery"
    IMAGE_UNAVAILABLE = "image_unavailable"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class DetectionResult:
    success: bool
    latitude: float
    longitude: float
    outcome: DetectionOutcome | None = None
    door_number: str | None = None
    confidence: int = 0
    heading: int | float | None = None
    pitch: int | None = None
    observation: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    error: str | None = None
    image_calls: int = 0
    metadata_calls: int = 0

    def __post_init__(self) -> None:
        if self.outcome is None:
            inferred = DetectionOutcome.FOUND if self.success else DetectionOutcome.FAILED
            object.__setattr__(self, "outcome", inferred)
        if self.success != (self.outcome is DetectionOutcome.FOUND):
            raise ValueError("success must be true exactly when outcome is found")

    def to_dict(self) -> dict:
        return asdict(self)
