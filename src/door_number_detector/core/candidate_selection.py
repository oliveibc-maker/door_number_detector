"""Shared candidate ranking and selection policy."""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from enum import IntEnum


@dataclass(frozen=True)
class Candidate:
    number: str
    confidence: int
    heading_offset: int | float | None = None
    pitch: int | None = None
    fov: int | None = None


@dataclass(frozen=True)
class Decision:
    """The chosen candidate, and whether it passes the confidence threshold."""

    candidate: Candidate
    accepted: bool


class Suspicion(IntEnum):
    CLEAN = 0
    YEAR_LIKE = 1
    WATERMARK_FRAGMENT = 2


class CandidateSelector:
    def __init__(self, confidence_threshold: int, logger: logging.Logger | None = None):
        self.confidence_threshold = confidence_threshold
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _suspicion(number: str) -> Suspicion:
        if number == "202" or re.fullmatch(r"(?:19|20|30)\d{2}", number):
            return Suspicion.YEAR_LIKE
        if number == "2" or re.fullmatch(r"2[0-6]", number):
            return Suspicion.WATERMARK_FRAGMENT
        return Suspicion.CLEAN

    def _qualifies(self, candidate: Candidate) -> bool:
        return bool(candidate.number) and candidate.confidence >= self.confidence_threshold

    def _ranked_candidates(self, candidates: list[Candidate]) -> list[Candidate]:
        votes = Counter(candidate.number for candidate in candidates if self._qualifies(candidate))
        return sorted(
            candidates,
            key=lambda candidate: (
                candidate.confidence,
                votes[candidate.number],
                len(candidate.number),
            ),
            reverse=True,
        )

    def best(self, candidates: list[Candidate]) -> Candidate | None:
        ranked = self._ranked_candidates(candidates)
        return ranked[0] if ranked else None

    def should_stop(self, candidates: list[Candidate]) -> bool:
        winner = self.best(candidates)
        if winner is None or not self._qualifies(winner):
            return False
        if self._suspicion(winner.number) is not Suspicion.CLEAN:
            self.logger.info(
                "Early stop skipped for suspicious candidate '%s' — continuing sweep.",
                winner.number,
            )
            return False
        return True

    def select(self, candidates: list[Candidate]) -> Decision | None:
        ranked = self._ranked_candidates(candidates)
        if not ranked:
            return None
        chosen = self._swap_suspicious(ranked)
        return Decision(chosen, accepted=self._qualifies(chosen))

    def _first_qualified(
        self, ranked: list[Candidate], suspicion: Suspicion, excluded: str
    ) -> Candidate | None:
        return next(
            (
                candidate
                for candidate in ranked
                if candidate.number != excluded
                and self._qualifies(candidate)
                and self._suspicion(candidate.number) is suspicion
            ),
            None,
        )

    def _swap_suspicious(self, ranked: list[Candidate]) -> Candidate:
        winner = ranked[0]
        suspicion = self._suspicion(winner.number)
        if suspicion is Suspicion.CLEAN:
            return winner

        clean = self._first_qualified(ranked, Suspicion.CLEAN, winner.number)
        if clean is not None:
            self.logger.info(
                "Replacing suspicious candidate '%s' with clean candidate '%s'.",
                winner.number,
                clean.number,
            )
            return clean

        if suspicion is Suspicion.YEAR_LIKE:
            fragment = self._first_qualified(ranked, Suspicion.WATERMARK_FRAGMENT, winner.number)
            if fragment is not None:
                self.logger.info(
                    "Replacing year-like candidate '%s' with watermark fragment '%s'.",
                    winner.number,
                    fragment.number,
                )
                return fragment

        self.logger.info(
            "Keeping suspicious candidate '%s' without a qualified alternative.", winner.number
        )
        return winner
