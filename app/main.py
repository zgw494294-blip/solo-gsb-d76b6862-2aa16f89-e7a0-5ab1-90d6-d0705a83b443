"""FastAPI application: offline HLS window reconciliation."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator

from .reconcile import ReconcileError, reconcile


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    playlist_url: str = Field(..., min_length=1)
    snapshots: list[str] = Field(..., min_length=2, max_length=30)

    @field_validator("playlist_url")
    @classmethod
    def _url_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("playlist_url must not be blank")
        return value

    @field_validator("snapshots")
    @classmethod
    def _snapshots_non_empty(cls, value: list[str]) -> list[str]:
        for i, snapshot in enumerate(value):
            if not snapshot or not snapshot.strip():
                raise ValueError(f"snapshot {i} is empty")
        return value


class SegmentOut(BaseModel):
    sequence: int
    uri: str
    duration: str
    discontinuity: int
    byte_offset: Optional[int] = None
    byte_length: Optional[int] = None
    start: str
    end: str


class ReconcileResponse(BaseModel):
    playlist_url: str
    count: int
    duration: str
    segments: list[SegmentOut]


class ErrorDetail(BaseModel):
    snapshot_index: Optional[int] = None
    sequence: Optional[int] = None


class ErrorResponse(BaseModel):
    error: str
    detail: str
    snapshot_index: Optional[int] = None
    sequence: Optional[int] = None


app = FastAPI(
    title="Offline HLS Window Reconciliation API",
    version="1.0.0",
    description=(
        "Reconstruct a contiguous segment timeline from ordered HLS media "
        "playlist snapshots, entirely offline."
    ),
)


def _error(code: str, detail: str, status: int = 422, **extra) -> JSONResponse:
    body: dict = {"error": code, "detail": detail}
    body.update({key: value for key, value in extra.items() if value is not None})
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(ReconcileError)
async def reconcile_error_handler(_request: Request, exc: ReconcileError) -> JSONResponse:
    return _error(
        exc.code,
        exc.message,
        snapshot_index=exc.snapshot_index,
        sequence=exc.sequence,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    safe_errors = [
        {
            "loc": [str(part) for part in error.get("loc", [])],
            "msg": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_request", "detail": safe_errors},
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/reconcile", response_model=ReconcileResponse)
async def reconcile_endpoint(payload: ReconcileRequest) -> ReconcileResponse:
    timeline = reconcile(payload.playlist_url, payload.snapshots)

    segments = [
        SegmentOut(
            sequence=seg.sequence,
            uri=seg.uri,
            duration=format(seg.duration, "f"),
            discontinuity=seg.discontinuity,
            byte_offset=seg.byte_offset,
            byte_length=seg.byte_length,
            start=format(seg.start, "f"),
            end=format(seg.end, "f"),
        )
        for seg in timeline
    ]
    total = timeline[-1].end if timeline else Decimal("0")
    return ReconcileResponse(
        playlist_url=payload.playlist_url,
        count=len(segments),
        duration=format(total, "f"),
        segments=segments,
    )
