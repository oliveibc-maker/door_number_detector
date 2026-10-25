"""Stable mappings from detection outcomes to persistence and batch contracts."""

from dataclasses import dataclass

from door_number_detector.core.models import DetectionOutcome


@dataclass(frozen=True)
class OutcomeDetails:
    csv_prediction_found: str
    observation: str
    database_status: str
    database_message: str | None
    persist: bool
    metrics_success: bool
    metrics_no_imagery: bool


OUTCOME_DETAILS = {
    DetectionOutcome.FOUND: OutcomeDetails("YES", "", "success", None, True, True, False),
    DetectionOutcome.LOW_CONFIDENCE: OutcomeDetails(
        "NO",
        "Low confidence; manual review recommended.",
        "error",
        "Low confidence; manual review recommended.",
        True,
        False,
        False,
    ),
    DetectionOutcome.NO_IMAGERY: OutcomeDetails(
        "NO_IMAGERY",
        "No Street View imagery available",
        "error",
        "No Street View imagery available",
        True,
        False,
        True,
    ),
    DetectionOutcome.IMAGE_UNAVAILABLE: OutcomeDetails(
        "NO", "Image unavailable", "error", "Image unavailable", True, False, False
    ),
    DetectionOutcome.CANCELLED: OutcomeDetails(
        "NO", "Detection cancelled", "error", "Detection cancelled", False, False, False
    ),
    DetectionOutcome.FAILED: OutcomeDetails(
        "NO", "Detection failed", "error", "Detection failed", True, False, False
    ),
}
