"""Shared candidate ranking and selection policy."""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    number: str
    confidence: int
    heading_offset: int | float | None = None
    pitch: int | None = None
    fov: int | None = None


class CandidateSelector:
    def __init__(self, confidence_threshold: int, logger: logging.Logger | None = None):
        self.confidence_threshold = confidence_threshold
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _suspicion_tier(number: str) -> int:
        if number == "202" or re.fullmatch(r"(?:19|20|30)\d{2}", number):
            return 1
        if number == "2" or re.fullmatch(r"2[0-6]", number):
            return 2
        return 0

    @staticmethod
    def _image_key(candidate: Candidate) -> tuple[int | float | None, int | None, int | None]:
        return candidate.heading_offset, candidate.pitch, candidate.fov

    def _ranked_candidates(self, candidates: list[Candidate]) -> list[Candidate]:
        image_votes: dict[str, set[tuple[int | float | None, int | None, int | None]]] = (
            defaultdict(set)
        )
        for candidate in candidates:
            if candidate.number and candidate.confidence >= self.confidence_threshold:
                image_votes[candidate.number].add(self._image_key(candidate))

        return sorted(
            candidates,
            key=lambda candidate: (
                candidate.confidence,
                len(image_votes[candidate.number]),
                len(candidate.number),
            ),
            reverse=True,
        )

    def best(self, candidates: list[Candidate]) -> Candidate | None:
        ranked = self._ranked_candidates(candidates)
        return ranked[0] if ranked else None

    def should_stop(self, candidates: list[Candidate]) -> bool:
        winner = self.best(candidates)
        if winner is None or winner.confidence < self.confidence_threshold:
            return False
        if self._suspicion_tier(winner.number):
            self.logger.info(
                "Early stop skipped for suspicious candidate '%s' — continuing sweep.",
                winner.number,
            )
            return False
        return bool(winner.number)

    def select(self, candidates: list[Candidate]) -> Candidate | None:
        ranked = self._ranked_candidates(candidates)
        winner = ranked[0] if ranked else None
        if winner is None or not self._suspicion_tier(winner.number):
            return winner

        clean = next(
            (
                candidate
                for candidate in ranked
                if candidate.number
                and candidate.number != winner.number
                and not self._suspicion_tier(candidate.number)
                and candidate.confidence >= self.confidence_threshold
            ),
            None,
        )
        if clean is not None:
            self.logger.info(
                "Replacing suspicious candidate '%s' with clean candidate '%s'.",
                winner.number,
                clean.number,
            )
            return clean

        if self._suspicion_tier(winner.number) == 1:
            tier_two = next(
                (
                    candidate
                    for candidate in ranked
                    if candidate.number
                    and candidate.number != winner.number
                    and self._suspicion_tier(candidate.number) == 2
                    and candidate.confidence >= self.confidence_threshold
                ),
                None,
            )
            if tier_two is not None:
                self.logger.info(
                    "Replacing year-like candidate '%s' with watermark fragment '%s'.",
                    winner.number,
                    tier_two.number,
                )
                return tier_two

        self.logger.info(
            "Keeping suspicious candidate '%s' without a qualified alternative.", winner.number
        )
        return winner
