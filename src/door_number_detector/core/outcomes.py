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


def _not_found(
    reason: str, *, token: str = "NO", persist: bool = True, no_imagery: bool = False
) -> OutcomeDetails:
    """A non-found outcome: the same safe reason goes to the CSV and the database."""
    return OutcomeDetails(
        csv_prediction_found=token,
        observation=reason,
        database_status="error",
        database_message=reason,
        persist=persist,
        metrics_success=False,
        metrics_no_imagery=no_imagery,
    )


OUTCOME_DETAILS = {
    DetectionOutcome.FOUND: OutcomeDetails(
        csv_prediction_found="YES",
        observation="",
        database_status="success",
        database_message=None,
        persist=True,
        metrics_success=True,
        metrics_no_imagery=False,
    ),
    DetectionOutcome.LOW_CONFIDENCE: _not_found("Low confidence; manual review recommended."),
    DetectionOutcome.NO_IMAGERY: _not_found(
        "No Street View imagery available", token="NO_IMAGERY", no_imagery=True
    ),
    DetectionOutcome.IMAGE_UNAVAILABLE: _not_found("Image unavailable"),
    DetectionOutcome.CANCELLED: _not_found("Detection cancelled", persist=False),
    DetectionOutcome.FAILED: _not_found("Detection failed"),
}
