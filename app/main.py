"""FastAPI application exposing the offline HLS reconciliation endpoint."""

from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI, HTTPException

from .models import (
    ByteRangeOut,
    ErrorResponse,
    ReconcileRequest,
    ReconcileResponse,
    SegmentOut,
)
from .parser import PlaylistParseError, parse_playlist
from .reconciler import ReconcileError, reconcile

MAX_SEGMENTS_PER_SNAPSHOT = 100

app = FastAPI(
    title="Offline HLS Window Reconciler",
    version="1.0.0",
    description=(
        "Reconstructs the full segment timeline of a live HLS window from "
        "ordered playlist snapshots. Purely offline: playlists are supplied "
        "in the request body and never fetched."
    ),
)


def _decimal(value: Decimal) -> str:
    """Render a Decimal in plain (non-scientific) decimal notation."""
    return format(value, "f")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/v1/reconcile",
    response_model=ReconcileResponse,
    responses={422: {"model": ErrorResponse, "description": "Validation or reconciliation failure"}},
)
def reconcile_endpoint(request: ReconcileRequest) -> ReconcileResponse:
    base_url = str(request.playlist_url)

    parsed_snapshots = []
    for index, snapshot in enumerate(request.snapshots):
        try:
            segments = parse_playlist(snapshot, base_url)
        except PlaylistParseError as exc:
            detail: dict = {
                "error": "invalid_playlist",
                "message": exc.message,
                "snapshot_index": index,
            }
            if exc.line is not None:
                detail["line"] = exc.line
            raise HTTPException(status_code=422, detail=detail) from exc
        if not segments:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "no_segments",
                    "message": f"snapshot {index} contains no segments; "
                    "each snapshot must contain 1-100 segments",
                    "snapshot_index": index,
                },
            )
        if len(segments) > MAX_SEGMENTS_PER_SNAPSHOT:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "too_many_segments",
                    "message": f"snapshot {index} contains {len(segments)} segments; "
                    f"the maximum is {MAX_SEGMENTS_PER_SNAPSHOT}",
                    "snapshot_index": index,
                    "segment_count": len(segments),
                },
            )
        parsed_snapshots.append(segments)

    try:
        merged = reconcile(parsed_snapshots)
    except ReconcileError as exc:
        raise HTTPException(status_code=422, detail=exc.to_detail()) from exc

    segments_out: list[SegmentOut] = []
    # The timeline origin inherits the decimal precision of the first
    # segment's duration, so start/end render at a consistent scale.
    cursor = Decimal(0).quantize(merged[0].duration)
    previous_discontinuity: int | None = None
    for segment in merged:
        start = cursor
        end = start + segment.duration
        segments_out.append(
            SegmentOut(
                sequence=segment.sequence,
                uri=segment.uri,
                duration=_decimal(segment.duration),
                start=_decimal(start),
                end=_decimal(end),
                discontinuity_sequence=segment.discontinuity_sequence,
                discontinuity=(
                    previous_discontinuity is not None
                    and segment.discontinuity_sequence != previous_discontinuity
                ),
                byte_range=(
                    ByteRangeOut(
                        length=segment.byte_range.length,
                        offset=segment.byte_range.offset,
                    )
                    if segment.byte_range is not None
                    else None
                ),
            )
        )
        cursor = end
        previous_discontinuity = segment.discontinuity_sequence

    return ReconcileResponse(
        playlist_url=base_url,
        snapshot_count=len(request.snapshots),
        segment_count=len(segments_out),
        start_sequence=merged[0].sequence,
        end_sequence=merged[-1].sequence,
        total_duration=_decimal(cursor),
        segments=segments_out,
    )
