"""Offline HLS media playlist parsing.

Parses the subset of RFC 8216 tags needed for window reconciliation:

- ``EXT-X-MEDIA-SEQUENCE``       media sequence number of the first segment
- ``EXTINF``                     exact decimal segment duration
- ``EXT-X-DISCONTINUITY-SEQUENCE``  base of the discontinuity counter
- ``EXT-X-DISCONTINUITY``        increments the discontinuity counter
- ``EXT-X-BYTERANGE``            byte range of a segment

All parsing is done in memory; no network I/O is performed. Segment URIs are
resolved against the supplied playlist URL. Durations are kept as
:class:`decimal.Decimal` so that timeline arithmetic is exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin


@dataclass(frozen=True)
class ByteRange:
    """Byte range of a segment, always fully resolved (length + offset)."""

    length: int
    offset: int


@dataclass(frozen=True)
class Segment:
    """One parsed media segment."""

    sequence: int
    uri: str
    duration: Decimal
    discontinuity_sequence: int
    byte_range: ByteRange | None


class PlaylistParseError(ValueError):
    """Raised when a snapshot playlist is malformed."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


def _parse_non_negative_int(value: str, tag: str, line: int) -> int:
    try:
        number = int(value, 10)
    except ValueError:
        raise PlaylistParseError(f"{tag}: invalid integer {value!r}", line) from None
    if number < 0:
        raise PlaylistParseError(f"{tag}: value must be non-negative, got {number}", line)
    return number


def _parse_duration(value: str, line: int) -> Decimal:
    try:
        duration = Decimal(value)
    except InvalidOperation:
        raise PlaylistParseError(f"EXTINF: invalid decimal duration {value!r}", line) from None
    if not duration.is_finite() or duration < 0:
        raise PlaylistParseError(
            f"EXTINF: duration must be a finite, non-negative decimal, got {value!r}", line
        )
    return duration


def _parse_byte_range(value: str, line: int) -> tuple[int, int | None]:
    parts = value.split("@")
    if len(parts) > 2 or not parts[0]:
        raise PlaylistParseError(f"EXT-X-BYTERANGE: invalid value {value!r}", line)
    length = _parse_non_negative_int(parts[0].strip(), "EXT-X-BYTERANGE", line)
    offset = None
    if len(parts) == 2:
        offset = _parse_non_negative_int(parts[1].strip(), "EXT-X-BYTERANGE", line)
    return length, offset


def parse_playlist(text: str, playlist_url: str) -> list[Segment]:
    """Parse one media playlist snapshot into an ordered list of segments.

    ``playlist_url`` is the base URL that relative segment URIs resolve
    against. Raises :class:`PlaylistParseError` on malformed input.
    """
    media_sequence = 0
    media_sequence_seen = False
    discontinuity_sequence_seen = False

    # Running discontinuity counter: starts at the EXT-X-DISCONTINUITY-SEQUENCE
    # base (default 0) and is incremented by every EXT-X-DISCONTINUITY marker.
    discontinuity_count = 0

    pending_duration: Decimal | None = None
    pending_byte_range: tuple[int, int | None] | None = None
    pending_discontinuity = False

    segments: list[Segment] = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            # Must be checked before the bare "#EXT-X-DISCONTINUITY" prefix.
            if segments:
                raise PlaylistParseError(
                    "EXT-X-DISCONTINUITY-SEQUENCE must appear before the first segment", line_no
                )
            if discontinuity_sequence_seen:
                raise PlaylistParseError("duplicate EXT-X-DISCONTINUITY-SEQUENCE tag", line_no)
            discontinuity_count = _parse_non_negative_int(
                line.split(":", 1)[1], "EXT-X-DISCONTINUITY-SEQUENCE", line_no
            )
            discontinuity_sequence_seen = True
            continue

        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            if segments:
                raise PlaylistParseError(
                    "EXT-X-MEDIA-SEQUENCE must appear before the first segment", line_no
                )
            if media_sequence_seen:
                raise PlaylistParseError("duplicate EXT-X-MEDIA-SEQUENCE tag", line_no)
            media_sequence = _parse_non_negative_int(
                line.split(":", 1)[1], "EXT-X-MEDIA-SEQUENCE", line_no
            )
            media_sequence_seen = True
            continue

        if line.startswith("#EXTINF:"):
            if pending_duration is not None:
                raise PlaylistParseError("EXTINF tag not followed by a segment URI", line_no)
            pending_duration = _parse_duration(line.split(":", 1)[1].split(",", 1)[0].strip(), line_no)
            continue

        if line.startswith("#EXT-X-BYTERANGE:"):
            if pending_byte_range is not None:
                raise PlaylistParseError("EXT-X-BYTERANGE tag not followed by a segment URI", line_no)
            pending_byte_range = _parse_byte_range(line.split(":", 1)[1].strip(), line_no)
            continue

        if line == "#EXT-X-DISCONTINUITY":
            pending_discontinuity = True
            continue

        if line.startswith("#"):
            # Other tags (EXTM3U, EXT-X-TARGETDURATION, EXT-X-ENDLIST, ...)
            # and comments carry no information needed for reconciliation.
            continue

        # Segment URI line.
        if pending_duration is None:
            raise PlaylistParseError("segment URI without a preceding EXTINF tag", line_no)

        if pending_discontinuity:
            discontinuity_count += 1

        uri = urljoin(playlist_url, line)

        byte_range: ByteRange | None = None
        if pending_byte_range is not None:
            length, offset = pending_byte_range
            if offset is not None:
                byte_range = ByteRange(length=length, offset=offset)
            else:
                # No explicit offset: only the immediately preceding segment of
                # the same manifest may be inherited from, and only when it
                # addresses the same (resolved) URI.
                previous = segments[-1] if segments else None
                if previous is None or previous.uri != uri or previous.byte_range is None:
                    raise PlaylistParseError(
                        "EXT-X-BYTERANGE without offset requires an immediately "
                        "preceding segment with the same URI and a byte range",
                        line_no,
                    )
                byte_range = ByteRange(
                    length=length,
                    offset=previous.byte_range.offset + previous.byte_range.length,
                )

        segments.append(
            Segment(
                sequence=media_sequence + len(segments),
                uri=uri,
                duration=pending_duration,
                discontinuity_sequence=discontinuity_count,
                byte_range=byte_range,
            )
        )
        pending_duration = None
        pending_byte_range = None
        pending_discontinuity = False

    if pending_duration is not None:
        raise PlaylistParseError("EXTINF tag not followed by a segment URI")
    if pending_byte_range is not None:
        raise PlaylistParseError("EXT-X-BYTERANGE tag not followed by a segment URI")

    return segments
