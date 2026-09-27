"""Request/response schemas for the reconciliation API."""

from __future__ import annotations

from pydantic import AnyHttpUrl, BaseModel, Field

MIN_SNAPSHOTS = 2
MAX_SNAPSHOTS = 30


class ReconcileRequest(BaseModel):
    """Body of ``POST /v1/reconcile``."""

    playlist_url: AnyHttpUrl = Field(
        ...,
        description="Absolute URL of the playlist. Segment URIs are resolved "
        "against it; no network fetch is performed.",
        examples=["https://cdn.example.com/live/stream.m3u8"],
    )
    snapshots: list[str] = Field(
        ...,
        min_length=MIN_SNAPSHOTS,
        max_length=MAX_SNAPSHOTS,
        description="Chronologically ordered media playlist snapshots "
        "(2-30), each containing 1-100 segments.",
    )


class ByteRangeOut(BaseModel):
    length: int
    offset: int


class SegmentOut(BaseModel):
    sequence: int = Field(..., description="Media sequence number of the segment.")
    uri: str = Field(..., description="Segment URI resolved against playlist_url.")
    duration: str = Field(..., description="Exact decimal duration in seconds.")
    start: str = Field(..., description="Exact decimal start offset in seconds.")
    end: str = Field(..., description="Exact decimal end offset in seconds.")
    discontinuity_sequence: int = Field(
        ..., description="Discontinuity counter value the segment belongs to."
    )
    discontinuity: bool = Field(
        ..., description="True when a discontinuity occurs between this segment "
        "and the previous one in the merged window."
    )
    byte_range: ByteRangeOut | None = Field(
        ..., description="Resolved byte range, or null when the playlist "
        "carried no EXT-X-BYTERANGE for the segment."
    )


class ReconcileResponse(BaseModel):
    playlist_url: str
    snapshot_count: int
    segment_count: int
    start_sequence: int
    end_sequence: int
    total_duration: str = Field(..., description="Exact decimal window duration in seconds.")
    segments: list[SegmentOut]


class ErrorDetail(BaseModel):
    """Body of the ``detail`` object of 422 reconciliation failures."""

    error: str = Field(..., description="Machine-readable error code.")
    message: str = Field(..., description="Human-readable explanation.")
    snapshot_index: int = Field(..., description="Zero-based index of the offending snapshot.")
    sequence: int | None = Field(None, description="Media sequence number involved, if any.")


class ErrorResponse(BaseModel):
    detail: ErrorDetail
