"""Tests for parsing, reconciliation and the HTTP API."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.hls import PlaylistParseError, parse_playlist
from app.main import app
from app.reconcile import ReconcileError, reconcile

client = TestClient(app)

BASE_URL = "https://cdn.example.com/live/index.m3u8"
SEG_URL = "https://cdn.example.com/live/seg.ts"


def m3u(segments, *, media_sequence=None, discont_sequence=None, discont_before=(),
        byteranges=None, uris=None):
    """Build a small media playlist.

    segments: number of segments (durations 1.10)
    discont_before: indexes (0-based within this playlist) flagged with
                    EXT-X-DISCONTINUITY.
    byteranges: {index: "100@0" | "100"} (None entry means unranged).
    uris: optional per-index URI strings.
    """

    lines = ["#EXTM3U"]
    if media_sequence is not None:
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}")
    if discont_sequence is not None:
        lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discont_sequence}")
    byteranges = byteranges or {}
    for i in range(segments):
        if i in discont_before:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append("#EXTINF:1.10,")
        if i in byteranges:
            lines.append(f"#EXT-X-BYTERANGE:{byteranges[i]}")
        uri = uris[i] if uris else "seg.ts"
        lines.append(uri)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- parser

def test_parse_defaults_sequences_and_discontinuity():
    segs = parse_playlist(m3u(3), BASE_URL)
    assert [s.sequence for s in segs] == [0, 1, 2]
    assert [s.discontinuity for s in segs] == [0, 0, 0]
    assert all(s.uri == SEG_URL for s in segs)
    assert all(s.duration == Decimal("1.10") for s in segs)


def test_parse_media_sequence_and_relative_uri_resolution():
    text = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:100",
        "#EXTINF:2.5,",
        "../v/clip.aac",
    ])
    segs = parse_playlist(text, "https://cdn.example.com/live/idx/index.m3u8")
    assert segs[0].sequence == 100
    assert segs[0].uri == "https://cdn.example.com/live/v/clip.aac"


def test_parse_discontinuity_counter_starts_at_base_and_increments():
    segs = parse_playlist(m3u(4, media_sequence=10, discont_sequence=5,
                              discont_before=(1, 3)), BASE_URL)
    assert [s.discontinuity for s in segs] == [5, 6, 6, 7]


def test_parse_byterange_explicit_offsets():
    pl = m3u(2, media_sequence=0, byteranges={0: "100@0", 1: "200@100"})
    segs = parse_playlist(pl, BASE_URL)
    assert (segs[0].byte_range.offset, segs[0].byte_range.length) == (0, 100)
    assert (segs[1].byte_range.offset, segs[1].byte_range.length) == (100, 200)


def test_parse_byterange_implicit_offset_tracks_same_uri_predecessor():
    text = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:100@0",
        "big.ts",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:50",
        "big.ts",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:25",
        "big.ts",
    ])
    segs = parse_playlist(text, BASE_URL)
    assert [s.byte_range.offset for s in segs] == [0, 100, 150]
    assert [s.byte_range.length for s in segs] == [100, 50, 25]


def test_parse_byterange_implicit_offset_rejected_for_different_uri():
    text = "\n".join([
        "#EXTM3U",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:100@0",
        "a.ts",
        "#EXTINF:1.0,",
        "#EXT-X-BYTERANGE:50",
        "b.ts",
    ])
    with pytest.raises(PlaylistParseError):
        parse_playlist(text, BASE_URL)


def test_parse_byterange_implicit_offset_rejected_as_first_segment():
    text = "\n".join(["#EXTM3U", "#EXTINF:1.0,", "#EXT-X-BYTERANGE:50", "a.ts"])
    with pytest.raises(PlaylistParseError):
        parse_playlist(text, BASE_URL)


def test_parse_rejects_bad_playlist():
    with pytest.raises(PlaylistParseError):
        parse_playlist("#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\na.ts\n", BASE_URL)
    with pytest.raises(PlaylistParseError):
        parse_playlist("", BASE_URL)
    with pytest.raises(PlaylistParseError):
        parse_playlist("#EXTM3U\n#EXTINF:x,\na.ts\n", BASE_URL)


# ------------------------------------------------------------- reconcile

def test_reconcile_sliding_window_with_overlap():
    snaps = [m3u(3, media_sequence=0), m3u(3, media_sequence=2), m3u(3, media_sequence=4)]
    timeline = reconcile(BASE_URL, snaps)
    assert [s.sequence for s in timeline] == [0, 1, 2, 3, 4, 5, 6]
    assert timeline[0].start == Decimal("0")
    assert timeline[1].start == Decimal("1.10")
    assert timeline[-1].end == Decimal("7.70")


def test_reconcile_exact_adjacent_windows():
    snaps = [m3u(2, media_sequence=0), m3u(2, media_sequence=2)]
    timeline = reconcile(BASE_URL, snaps)
    assert [s.sequence for s in timeline] == [0, 1, 2, 3]


def test_reconcile_rejects_rollback():
    snaps = [m3u(2, media_sequence=10), m3u(2, media_sequence=9)]
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, snaps)
    assert exc.value.code == "sequence_rollback"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 9


def test_reconcile_rejects_gap():
    snaps = [m3u(2, media_sequence=0), m3u(2, media_sequence=5)]
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, snaps)
    assert exc.value.code == "sequence_gap"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 5


def test_reconcile_rejects_overlap_conflict_on_duration():
    s0 = m3u(3, media_sequence=0)
    s1 = m3u(3, media_sequence=1)
    # Change duration of sequence 1 (overlap region) in the second snapshot.
    s1 = s1.replace("#EXTINF:1.10,", "#EXTINF:9.00,", 1)
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, [s0, s1])
    assert exc.value.code == "overlap_conflict"
    assert exc.value.snapshot_index == 1
    assert exc.value.sequence == 1


def test_reconcile_rejects_overlap_conflict_on_discontinuity():
    s0 = m3u(3, media_sequence=0)
    s1 = m3u(3, media_sequence=1, discont_before=(0,))
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, [s0, s1])
    assert exc.value.code == "overlap_conflict"
    assert exc.value.sequence == 1


def test_reconcile_rejects_overlap_conflict_on_byte_range():
    s0 = m3u(2, media_sequence=0, byteranges={0: "100@0", 1: "100@100"})
    s1 = m3u(2, media_sequence=1, byteranges={0: "100@100"})  # seq 1 same
    s1 = s1.replace("#EXT-X-BYTERANGE:100@100", "#EXT-X-BYTERANGE:100@200")
    with pytest.raises(ReconcileError) as exc:
        reconcile(BASE_URL, [s0, s1])
    assert exc.value.code == "overlap_conflict"
    assert exc.value.sequence == 1


def test_reconcile_accepts_identical_overlap_with_discontinuity():
    s0 = m3u(4, media_sequence=10, discont_sequence=2, discont_before=(2,))
    s1 = m3u(3, media_sequence=12, discont_sequence=2, discont_before=(0,))
    timeline = reconcile(BASE_URL, [s0, s1])
    by_seq = {s.sequence: s for s in timeline}
    assert by_seq[11].discontinuity == 2
    assert by_seq[12].discontinuity == 3
    assert by_seq[13].discontinuity == 3
    assert by_seq[14].discontinuity == 3


def test_reconcile_decimal_precision_is_exact():
    text = "\n".join([
        "#EXTM3U",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXTINF:0.1,", "a.ts",
        "#EXTINF:0.2,", "a.ts",
        "#EXTINF:0.3,", "a.ts",
    ])
    timeline = reconcile(BASE_URL, [text])
    assert [format(s.end, "f") for s in timeline] == ["0.1", "0.3", "0.6"]


# ------------------------------------------------------------------ API

def post(snapshots, url=BASE_URL):
    return client.post("/v1/reconcile", json={"playlist_url": url, "snapshots": snapshots})


def test_api_happy_path():
    resp = post([m3u(3, media_sequence=0), m3u(3, media_sequence=2)])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 5
    assert body["duration"] == "5.50"
    assert body["segments"][0] == {
        "sequence": 0,
        "uri": SEG_URL,
        "duration": "1.10",
        "discontinuity": 0,
        "byte_offset": None,
        "byte_length": None,
        "start": "0",
        "end": "1.10",
    }


def test_api_rollback_returns_422_with_snapshot_and_sequence():
    resp = post([m3u(2, media_sequence=10), m3u(2, media_sequence=9)])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "sequence_rollback"
    assert body["snapshot_index"] == 1
    assert body["sequence"] == 9


def test_api_overlap_conflict_returns_422_with_snapshot_and_sequence():
    s0 = m3u(2, media_sequence=0)
    s1 = m3u(2, media_sequence=1).replace("#EXTINF:1.10,", "#EXTINF:2.20,", 1)
    resp = post([s0, s1])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "overlap_conflict"
    assert body["snapshot_index"] == 1
    assert body["sequence"] == 1


def test_api_gap_returns_422():
    resp = post([m3u(1, media_sequence=0), m3u(1, media_sequence=7)])
    assert resp.status_code == 422
    assert resp.json()["error"] == "sequence_gap"


def test_api_invalid_playlist_returns_422():
    resp = post(["#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\nx.ts\n", m3u(2, media_sequence=0)])
    assert resp.status_code == 422
    assert resp.json()["snapshot_index"] == 0


def test_api_rejects_wrong_snapshot_count():
    assert post([m3u(1)]).status_code == 422  # only one snapshot
    assert post([m3u(1)] * 31).status_code == 422


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}
