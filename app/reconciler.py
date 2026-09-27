"""Cross-snapshot reconciliation of a sliding HLS media window.

Snapshots are merged by media sequence number. Overlapping segments must be
byte-for-byte consistent on the fields that identity a segment carries:
resolved URI, exact decimal duration, byte range and discontinuity count.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .parser import Segment


class ReconcileError(ValueError):
    """Raised when snapshots cannot be reconciled into a single window."""

    def __init__(
        self,
        error: str,
        message: str,
        snapshot_index: int,
        sequence: int | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.snapshot_index = snapshot_index
        self.sequence = sequence
        self.extra = extra

    def to_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "error": self.error,
            "message": self.message,
            "snapshot_index": self.snapshot_index,
        }
        if self.sequence is not None:
            detail["sequence"] = self.sequence
        detail.update(self.extra)
        return detail


def _format_byte_range(segment: Segment) -> dict[str, int] | None:
    if segment.byte_range is None:
        return None
    return {"length": segment.byte_range.length, "offset": segment.byte_range.offset}


def _first_conflict(merged: Segment, candidate: Segment) -> tuple[str, Any, Any] | None:
    """Return ``(field, expected, actual)`` for the first mismatch, or None."""
    if merged.uri != candidate.uri:
        return "uri", merged.uri, candidate.uri
    if merged.duration != candidate.duration:
        return (
            "duration",
            format(merged.duration, "f"),
            format(candidate.duration, "f"),
        )
    if merged.byte_range != candidate.byte_range:
        return "byte_range", _format_byte_range(merged), _format_byte_range(candidate)
    if merged.discontinuity_sequence != candidate.discontinuity_sequence:
        return (
            "discontinuity_sequence",
            merged.discontinuity_sequence,
            candidate.discontinuity_sequence,
        )
    return None


def reconcile(snapshots: list[list[Segment]]) -> list[Segment]:
    """Merge ordered per-snapshot segment lists into one continuous window.

    Raises :class:`ReconcileError` when a snapshot's start sequence regresses,
    when a snapshot starts past the end of the merged window (a gap), or when
    an overlapping segment disagrees with the already merged state.
    """
    merged: dict[int, Segment] = {}
    previous_start: int | None = None
    last_sequence: int | None = None

    for index, segments in enumerate(snapshots):
        start = segments[0].sequence
        end = segments[-1].sequence

        if previous_start is not None and start < previous_start:
            raise ReconcileError(
                error="start_sequence_regression",
                message=(
                    f"snapshot {index} starts at media sequence {start}, which is "
                    f"before the previous snapshot's start {previous_start}"
                ),
                snapshot_index=index,
                sequence=start,
                previous_start=previous_start,
            )

        if last_sequence is not None and start > last_sequence + 1:
            raise ReconcileError(
                error="sequence_gap",
                message=(
                    f"snapshot {index} starts at media sequence {start}, but the "
                    f"merged window ends at {last_sequence}; the start must be at "
                    f"most {last_sequence + 1}"
                ),
                snapshot_index=index,
                sequence=start,
                merged_end_sequence=last_sequence,
            )

        for segment in segments:
            existing = merged.get(segment.sequence)
            if existing is None:
                merged[segment.sequence] = segment
                continue
            conflict = _first_conflict(existing, segment)
            if conflict is not None:
                field, expected, actual = conflict
                raise ReconcileError(
                    error="overlap_conflict",
                    message=(
                        f"snapshot {index} conflicts with the merged window at "
                        f"media sequence {segment.sequence}: {field} differs"
                    ),
                    snapshot_index=index,
                    sequence=segment.sequence,
                    field=field,
                    expected=expected,
                    actual=actual,
                )

        previous_start = start
        last_sequence = end if last_sequence is None else max(last_sequence, end)

    return [merged[sequence] for sequence in sorted(merged)]
