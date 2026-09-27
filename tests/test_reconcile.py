"""End-to-end tests for the offline HLS window reconciliation API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

BASE_URL = "https://cdn.example.com/live/stream.m3u8"

client = TestClient(app)


def playlist(
    segments,
    media_sequence=0,
    discontinuity_sequence=None,
    target_duration=8,
):
    """Build a media playlist. ``segments`` is a list of dicts with keys:
    duration, uri, discontinuity (bool), byte_range ("n" or "n@o")."""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
    ]
    if discontinuity_sequence is not None:
        lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_sequence}")
    for seg in segments:
        if seg.get("discontinuity"):
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{seg['duration']},")
        if seg.get("byte_range") is not None:
            lines.append(f"#EXT-X-BYTERANGE:{seg['byte_range']}")
        lines.append(seg["uri"])
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def segs(start, count, duration="8.0", prefix="seg", suffix=".ts", **extra):
    return [
        {"duration": duration, "uri": f"{prefix}{n}{suffix}", **extra}
        for n in range(start, start + count)
    ]


def post(snapshots, playlist_url=BASE_URL):
    return client.post(
        "/v1/reconcile", json={"playlist_url": playlist_url, "snapshots": snapshots}
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_basic_merge_produces_exact_timeline():
    snap1 = playlist(segs(100, 4), media_sequence=100)
    snap2 = playlist(segs(102, 4), media_sequence=102)

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    body = resp.json()
    assert body["segment_count"] == 6
    assert body["start_sequence"] == 100
    assert body["end_sequence"] == 105
    assert body["total_duration"] == "48.0"
    assert [s["sequence"] for s in body["segments"]] == [100, 101, 102, 103, 104, 105]
    assert [(s["start"], s["end"]) for s in body["segments"]] == [
        ("0.0", "8.0"),
        ("8.0", "16.0"),
        ("16.0", "24.0"),
        ("24.0", "32.0"),
        ("32.0", "40.0"),
        ("40.0", "48.0"),
    ]


def test_adjacent_snapshots_without_overlap():
    snap1 = playlist(segs(0, 2), media_sequence=0)
    snap2 = playlist(segs(2, 2), media_sequence=2)

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    assert [s["sequence"] for s in resp.json()["segments"]] == [0, 1, 2, 3]


def test_fully_contained_snapshot_is_accepted():
    snap1 = playlist(segs(10, 5), media_sequence=10)
    snap2 = playlist(segs(11, 3), media_sequence=11)

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    assert resp.json()["segment_count"] == 5


def test_exact_decimal_durations_are_preserved():
    snap1 = playlist(
        [
            {"duration": "6.006", "uri": "a.ts"},
            {"duration": "5.995", "uri": "b.ts"},
        ],
        media_sequence=0,
    )
    snap2 = playlist(
        [
            {"duration": "5.995", "uri": "b.ts"},
            {"duration": "6.0060", "uri": "c.ts"},
        ],
        media_sequence=1,
    )

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert [(s["duration"], s["start"], s["end"]) for s in segments] == [
        ("6.006", "0.000", "6.006"),
        ("5.995", "6.006", "12.001"),
        ("6.0060", "12.001", "18.0070"),
    ]
    assert resp.json()["total_duration"] == "18.0070"


def test_relative_and_absolute_uri_resolution():
    snap1 = playlist(
        [
            {"duration": "4", "uri": "media/seg0.ts"},
            {"duration": "4", "uri": "../shared/seg1.ts"},
            {"duration": "4", "uri": "/abs/seg2.ts"},
            {"duration": "4", "uri": "https://other.example.com/seg3.ts"},
        ],
        media_sequence=0,
    )
    snap2 = playlist(segs(4, 1, duration="4"), media_sequence=4)

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    assert [s["uri"] for s in resp.json()["segments"]] == [
        "https://cdn.example.com/live/media/seg0.ts",
        "https://cdn.example.com/shared/seg1.ts",
        "https://cdn.example.com/abs/seg2.ts",
        "https://other.example.com/seg3.ts",
        "https://cdn.example.com/live/seg4.ts",
    ]


# ---------------------------------------------------------------------------
# Discontinuity handling
# ---------------------------------------------------------------------------


def test_discontinuity_counter_from_base_and_markers():
    snap1 = playlist(
        [
            {"duration": "8", "uri": "s10.ts"},
            {"duration": "8", "uri": "s11.ts"},
            {"duration": "8", "uri": "s12.ts", "discontinuity": True},
            {"duration": "8", "uri": "s13.ts"},
        ],
        media_sequence=10,
        discontinuity_sequence=2,
    )
    snap2 = playlist(
        [
            {"duration": "8", "uri": "s12.ts", "discontinuity": True},
            {"duration": "8", "uri": "s13.ts"},
            {"duration": "8", "uri": "s14.ts"},
        ],
        media_sequence=12,
        discontinuity_sequence=2,
    )

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert [s["discontinuity_sequence"] for s in segments] == [2, 2, 3, 3, 3]
    assert [s["discontinuity"] for s in segments] == [False, False, True, False, False]


def test_discontinuity_base_rolls_when_window_slides_past_marker():
    # Snapshot 1: base 2, marker before seq 11 -> seq 11/12 have count 3.
    snap1 = playlist(
        [
            {"duration": "8", "uri": "s10.ts"},
            {"duration": "8", "uri": "s11.ts", "discontinuity": True},
            {"duration": "8", "uri": "s12.ts"},
        ],
        media_sequence=10,
        discontinuity_sequence=2,
    )
    # Snapshot 2: marker slid out of the window, base bumped to 3.
    snap2 = playlist(
        [
            {"duration": "8", "uri": "s12.ts"},
            {"duration": "8", "uri": "s13.ts"},
        ],
        media_sequence=12,
        discontinuity_sequence=3,
    )

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    assert [s["discontinuity_sequence"] for s in resp.json()["segments"]] == [2, 3, 3, 3]


def test_default_discontinuity_base_is_zero():
    snap1 = playlist(segs(0, 2, duration="8"), media_sequence=0)
    snap2 = playlist(segs(1, 2, duration="8"), media_sequence=1)

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    assert [s["discontinuity_sequence"] for s in resp.json()["segments"]] == [0, 0, 0]


# ---------------------------------------------------------------------------
# Byte ranges
# ---------------------------------------------------------------------------


def test_byte_range_explicit_and_inherited_offsets():
    snap1 = playlist(
        [
            {"duration": "4", "uri": "video.ts", "byte_range": "1000@700"},
            {"duration": "4", "uri": "video.ts", "byte_range": "1200"},
            {"duration": "4", "uri": "video.ts", "byte_range": "800"},
        ],
        media_sequence=0,
    )
    snap2 = playlist(
        [
            {"duration": "4", "uri": "video.ts", "byte_range": "1200@1700"},
            {"duration": "4", "uri": "video.ts", "byte_range": "800"},
            {"duration": "4", "uri": "video.ts", "byte_range": "900"},
        ],
        media_sequence=1,
    )

    resp = post([snap1, snap2])

    assert resp.status_code == 200
    assert [s["byte_range"] for s in resp.json()["segments"]] == [
        {"length": 1000, "offset": 700},
        {"length": 1200, "offset": 1700},
        {"length": 800, "offset": 2900},
        {"length": 900, "offset": 3700},
    ]


def test_byte_range_without_offset_requires_predecessor():
    snap1 = playlist(
        [{"duration": "4", "uri": "video.ts", "byte_range": "1000"}],
        media_sequence=0,
    )
    snap2 = playlist(segs(1, 1, duration="4"), media_sequence=1)

    resp = post([snap1, snap2])

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_playlist"
    assert detail["snapshot_index"] == 0
    assert detail["line"] == 7  # the URI line where the offset-less range resolves


def test_byte_range_inheritance_requires_same_uri():
    snap1 = playlist(
        [
            {"duration": "4", "uri": "a.ts", "byte_range": "1000@0"},
            {"duration": "4", "uri": "b.ts", "byte_range": "500"},
        ],
        media_sequence=0,
    )
    snap2 = playlist(segs(2, 1, duration="4"), media_sequence=2)

    resp = post([snap1, snap2])

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_playlist"


def test_byte_range_inheritance_does_not_cross_manifest_boundary():
    snap1 = playlist(
        [{"duration": "4", "uri": "video.ts", "byte_range": "1000@0"}],
        media_sequence=0,
    )
    # Same URI, but the offset-less range is the first segment of *this* manifest.
    snap2 = playlist(
        [{"duration": "4", "uri": "video.ts", "byte_range": "1000"}],
        media_sequence=1,
    )

    resp = post([snap1, snap2])

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_playlist"
    assert detail["snapshot_index"] == 1


# ---------------------------------------------------------------------------
# Merge failures -> 422 with snapshot index and sequence
# ---------------------------------------------------------------------------


def test_start_sequence_regression():
    snap1 = playlist(segs(100, 3), media_sequence=100)
    snap2 = playlist(segs(99, 4), media_sequence=99)

    resp = post([snap1, snap2])

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "start_sequence_regression"
    assert detail["snapshot_index"] == 1
    assert detail["sequence"] == 99
    assert detail["previous_start"] == 100


def test_sequence_gap():
    snap1 = playlist(segs(100, 3), media_sequence=100)
    snap2 = playlist(segs(104, 2), media_sequence=104)

    resp = post([snap1, snap2])

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "sequence_gap"
    assert detail["snapshot_index"] == 1
    assert detail["sequence"] == 104
    assert detail["merged_end_sequence"] == 102


@pytest.mark.parametrize(
    "field,seg_a,seg_b",
    [
        ("uri", {"duration": "8", "uri": "s1.ts"}, {"duration": "8", "uri": "renamed.ts"}),
        ("duration", {"duration": "8", "uri": "s1.ts"}, {"duration": "8.5", "uri": "s1.ts"}),
        (
            "byte_range",
            {"duration": "8", "uri": "v.ts", "byte_range": "100@0"},
            {"duration": "8", "uri": "v.ts", "byte_range": "100@50"},
        ),
        (
            "byte_range",
            {"duration": "8", "uri": "v.ts", "byte_range": "100@0"},
            {"duration": "8", "uri": "v.ts"},
        ),
        (
            "discontinuity_sequence",
            {"duration": "8", "uri": "s1.ts"},
            {"duration": "8", "uri": "s1.ts", "discontinuity": True},
        ),
    ],
)
def test_overlap_conflicts(field, seg_a, seg_b):
    snap1 = playlist([{"duration": "8", "uri": "s0.ts"}, seg_a], media_sequence=0)
    snap2 = playlist([seg_b, {"duration": "8", "uri": "s2.ts"}], media_sequence=1)

    resp = post([snap1, snap2])

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "overlap_conflict"
    assert detail["snapshot_index"] == 1
    assert detail["sequence"] == 1
    assert detail["field"] == field
    assert "expected" in detail and "actual" in detail


def test_numerically_equal_durations_do_not_conflict():
    snap1 = playlist([{"duration": "8.0", "uri": "s0.ts"}], media_sequence=0)
    snap2 = playlist([{"duration": "8.00", "uri": "s0.ts"}], media_sequence=0)

    resp = post([snap1, snap2])

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_snapshot_count_bounds():
    one = post([playlist(segs(0, 1))])
    assert one.status_code == 422

    thirty_one = post([playlist(segs(0, 1), media_sequence=0)] * 31)
    assert thirty_one.status_code == 422

    thirty = post([playlist(segs(0, 1), media_sequence=0)] * 30)
    assert thirty.status_code == 200


def test_snapshot_segment_count_bounds():
    empty = post([playlist(segs(0, 1)), playlist([], media_sequence=1)])
    assert empty.status_code == 422
    assert empty.json()["detail"]["error"] == "no_segments"
    assert empty.json()["detail"]["snapshot_index"] == 1

    ok = post([playlist(segs(0, 100), media_sequence=0), playlist(segs(100, 1), media_sequence=100)])
    assert ok.status_code == 200

    too_many = post(
        [playlist(segs(0, 101), media_sequence=0), playlist(segs(101, 1), media_sequence=101)]
    )
    assert too_many.status_code == 422
    detail = too_many.json()["detail"]
    assert detail["error"] == "too_many_segments"
    assert detail["snapshot_index"] == 0
    assert detail["segment_count"] == 101


def test_invalid_playlist_url_rejected():
    resp = post([playlist(segs(0, 1)), playlist(segs(1, 1))], playlist_url="not-a-url")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Malformed playlists
# ---------------------------------------------------------------------------


def test_uri_without_extinf():
    bad = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\nseg0.ts\n"
    good = playlist(segs(1, 1), media_sequence=1)

    resp = post([bad, good])

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_playlist"
    assert detail["snapshot_index"] == 0
    assert detail["line"] == 3


def test_invalid_extinf_duration():
    bad = "#EXTM3U\n#EXTINF:abc,\nseg0.ts\n"
    good = playlist(segs(1, 1), media_sequence=1)

    resp = post([bad, good])

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_playlist"


def test_trailing_extinf_without_uri():
    bad = playlist(segs(0, 1), media_sequence=0) + "#EXTINF:8.0,\n"
    good = playlist(segs(1, 1), media_sequence=1)

    resp = post([bad, good])

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_playlist"


def test_media_sequence_after_segments_rejected():
    bad = "#EXTM3U\n#EXTINF:8,\nseg0.ts\n#EXT-X-MEDIA-SEQUENCE:5\n"
    good = playlist(segs(1, 1), media_sequence=1)

    resp = post([bad, good])

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_playlist"


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_health_endpoint():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_many_snapshot_chain():
    snapshots = [playlist(segs(n, 3, duration="2.5"), media_sequence=n) for n in range(0, 25, 2)]
    # starts: 0,2,4,...,24 each covering [n, n+2]; merged end = 26
    resp = post(snapshots)

    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_count"] == 13
    assert body["segment_count"] == 27
    assert body["end_sequence"] == 26
    assert body["total_duration"] == "67.5"
